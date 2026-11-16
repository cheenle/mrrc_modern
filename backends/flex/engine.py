"""The FlexRadio device engine: the only owner of the sockets and the queues.

Split of responsibility (design §4.4):

* ``session`` owns the wire; it hands us parsed status lines and raw datagrams.
* ``engine`` turns those into the few things the rest of the box needs: the
  latest 850-bin spectrum, a bounded RX audio ring, a 10 ms TX pacer, the meter
  table by name, and the radio's own description of itself.
* ``audio`` / ``scope`` adapt those onto the existing ``AudioHandler`` /
  ``ScopeProducer`` contracts.  ``backend`` translates CAT methods into
  commands.  Nothing here imports ``server`` or ``radio_state``.

Two numbers from the design are wired in on purpose: the pan is asked for
``xpixels=850 ypixels=256 min_dbm=-120 max_dbm=0`` so that a UI byte is exactly
``255 - pixel`` (§6), and the TX pacer sends one Opus packet every 10 ms
(§5.2/§6).
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

import numpy as np

from backends.flex import protocol, vita
from backends.flex.protocol import Status
from backends.flex.session import (
    DEFAULT_PORT, SmartSDRSession, SessionError, XMIT_CONFIRM_S, next_backoff,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SmartSDREngine", "discover_radios", "DiscoveryUnavailable",
    "XPIXELS", "YPIXELS", "DEFAULT_FPS", "DEFAULT_BANDWIDTH_HZ",
    "TX_PACKET_INTERVAL_S", "TX_QUEUE_PACKETS", "RX_QUEUE_SECONDS",
]

XPIXELS = 850
YPIXELS = 256
DEFAULT_FPS = 15
DEFAULT_BANDWIDTH_HZ = 100_000
RX_SAMPLE_RATE = 24_000
TX_PACKET_INTERVAL_S = 0.010          # one 10 ms Opus frame per tick
TX_QUEUE_PACKETS = 20                 # 200 ms of transmit audio
RX_QUEUE_SECONDS = 1.0                # bounded receive ring

_PAN_ID_FALLBACK = 0x40000000
_TX_STREAM_FALLBACK = 0x0B000002
_RX_STREAM_FALLBACK = 0x04000000
_HEX_TOKEN = re.compile(r"0x([0-9A-Fa-f]{6,8})")
_BARE_HEX = re.compile(r"[0-9A-Fa-f]{6,8}")


class DiscoveryUnavailable(RuntimeError):
    """UDP 4992 could not be bound, so discovery cannot run."""


def _first_int(text: str) -> Optional[int]:
    """The id in a create-command reply body.

    Two shapes are measured on a FLEX-6600 (v4.3.16, 2026-11-16), and the second
    one used to be read as decimal — which produced stream ids that match no
    VITA packet at all (RX audio still played because the radio pushes it
    regardless, but TX audio addressed a stream the radio does not have):

    * ``display panafall create`` → ``0x40000002,0x42000002`` (prefixed)
    * ``stream create …`` → ``4000008`` / ``84000000`` — **hex digits with the
      leading zero stripped and no prefix**, i.e. 0x04000008 / 0x84000000.

    A bare token is therefore read as hex when it is six to eight hex digits;
    shorter all-digit tokens (slice indexes, sequence numbers) stay decimal.
    """
    match = _HEX_TOKEN.search(text or "")
    if match:
        return int(match.group(1), 16)
    for token in (text or "").replace("|", " ").replace(",", " ").split():
        if _BARE_HEX.fullmatch(token):
            return int(token, 16)
    for token in (text or "").replace("|", " ").split():
        if token.isdigit():
            return int(token)
    return None


def _mhz(hz: float) -> str:
    return f"{float(hz) / 1e6:.6f}"


async def discover_radios(timeout: float = 1.5,
                          port: int = DEFAULT_PORT) -> List[Dict[str, str]]:
    """Listen for the radios' once-per-second UDP broadcast on port 4992.

    The box does not need to know the radio's address: it announces itself as a
    VITA ext-data packet with class ``0x534CFFFF`` and stream ``0x800`` whose
    payload is ``model=… serial=… version=… name=… callsign=… ip=… port=…``
    (analysis §1).  Raises ``DiscoveryUnavailable`` when the port cannot be
    bound (another MRRC instance already holds it, for instance).

    The receive path goes through a datagram endpoint rather than
    ``loop.sock_recvfrom``: uvloop's loop class advertises that method and then
    raises ``NotImplementedError`` when it is awaited (measured on the box's
    uvloop 0.23.0, 2026-11-16).  A datagram endpoint gives us the same peer
    address — which the address-less ``sock_recv`` would lose — on every loop
    implementation.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", int(port)))
    except OSError as exc:
        sock.close()
        raise DiscoveryUnavailable(f"cannot listen on UDP {port}: {exc}") from exc
    sock.setblocking(False)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, float(timeout))
    queue: "asyncio.Queue[Any]" = asyncio.Queue()
    found: Dict[str, Dict[str, str]] = {}
    transport = None
    try:
        # ``sock=`` hands the socket (and its SO_REUSEADDR) to the transport, so
        # the ownership of the close moves with it.
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _DiscoveryProtocol(queue), sock=sock)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                data, addr = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            payload = vita.parse_discovery_payload_bytes(data)
            if not payload:
                continue
            info = protocol.parse_discovery_payload(
                payload.decode("ascii", "replace"))
            if not info:
                continue
            info.setdefault("ip", addr[0])
            key = info.get("serial") or info.get("ip") or addr[0]
            found[key] = info
    finally:
        if transport is not None:
            transport.close()
        else:
            sock.close()
    return [found[key] for key in sorted(found)]


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Hand every broadcast datagram (with its source) to ``discover_radios``."""

    def __init__(self, queue: "asyncio.Queue[Any]") -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self._queue.put_nowait((data, addr))

    def error_received(self, exc: Exception) -> None:      # a bad broadcast is not fatal
        logger.debug("FlexRadio discovery socket error: %s", exc)


class SmartSDREngine:
    """Everything one FlexRadio session needs, in one place."""

    def __init__(self, host: str = "", port: int = DEFAULT_PORT, *,
                 program: str = "MRRC", station: str = "MRRC",
                 fps: int = DEFAULT_FPS, bandwidth_hz: int = DEFAULT_BANDWIDTH_HZ,
                 on_broadcast: Optional[Callable[[str, Any], None]] = None,
                 session_factory: Optional[Callable[[str, int], SmartSDRSession]] = None,
                 discovery_timeout: float = 1.5) -> None:
        self.host = str(host or "").strip()
        self.port = int(port)
        self.fps = int(fps)
        self.bandwidth_hz = int(bandwidth_hz)
        self.discovery_timeout = float(discovery_timeout)
        self._session_factory = session_factory
        self._on_broadcast = on_broadcast

        self.session: Optional[SmartSDRSession] = None
        self.connected = False
        self.connect_attempts = 0
        self.discovered: List[Dict[str, str]] = []

        # radio description
        self.model = ""
        self.serial = ""
        self.software_version = ""
        self.has_atu = False
        self.tx_timeout_s: Optional[float] = None

        # pan / streams
        self.pan_id = 0
        self.waterfall_id = 0
        #: ``display pan <id>`` state, keyed by pan id.  Needed to tell *our*
        #: panadapter from another client's when ``create`` is refused.
        self.pans: Dict[int, Dict[str, Any]] = {}
        self.rx_stream_id = 0
        self.tx_stream_id = 0
        self.pan_center_hz = 0
        self.rx_stream_ready = False
        self.tx_stream_ready = False

        # state
        self.slices: Dict[int, Dict[str, Any]] = {}
        self.own_slice: Optional[int] = None
        self.meter_defs: Dict[int, protocol.MeterDefinition] = {}
        self.meter_values: Dict[int, int] = {}

        # spectrum
        self._assembler = vita.FftAssembler()
        self._spectrum: Optional[np.ndarray] = None
        self.spectrum_frames = 0
        self.spectrum_errors = 0
        #: datagrams dropped because their stream id belongs to another client's
        #: panadapter / audio stream (a shared radio is the normal case)
        self.foreign_packets = 0

        # audio
        self.rx_audio: Deque[np.ndarray] = deque()
        self.rx_samples = 0
        self.rx_drops = 0
        self.audio_packets = 0

        # transmit
        self.tx_active = False
        self.tx_drops = 0
        self.tx_packets_sent = 0
        self.tx_underruns = 0
        self._tx_queue: Deque[bytes] = deque()
        self._tx_task: Optional[asyncio.Task] = None
        self._tx_deadline = 0.0

        self._running = False
        self._reconnect_task: Optional[asyncio.Task] = None

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> bool:
        """Discover (when no host is configured), connect, set the box up."""
        if not self.host:
            try:
                self.discovered = await discover_radios(self.discovery_timeout)
            except DiscoveryUnavailable as exc:
                logger.warning("FlexRadio discovery unavailable: %s", exc)
                self.discovered = []
            if not self.discovered:
                logger.warning("no FlexRadio answered on UDP %d; set "
                               "MRRC_FLEX_HOST to the radio's address", self.port)
                return False
            self.host = self.discovered[0].get("ip", "")
            logger.info("discovered %d FlexRadio(s); using %s (%s)",
                        len(self.discovered), self.host,
                        self.discovered[0].get("model", "?"))
        ok = await self._connect_once()
        if ok:
            self._running = True
            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop(), name="flex-reconnect")
        return ok

    async def _connect_once(self) -> bool:
        factory = self._session_factory or (
            lambda host, port: SmartSDRSession(
                host, port, program="MRRC", station="MRRC"))
        session = factory(self.host, self.port)
        session._on_status = self._handle_status
        session._on_packet = self._handle_packet
        session._on_interlock = self._handle_interlock
        self.connect_attempts += 1
        if not await session.connect():
            return False
        self.session = session
        self.connected = True
        self.connect_attempts = 0
        await self._probe_radio()
        await self.ensure_pan()
        await self.ensure_slice()
        await self.ensure_streams()
        return True

    async def stop(self) -> None:
        self._running = False
        task = self._reconnect_task
        self._reconnect_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass
        await self._stop_tx_task()
        self.tx_active = False
        session = self.session
        self.session = None
        self.connected = False
        if session is not None:
            await session.close()
        self.rx_audio.clear()
        self.rx_samples = 0

    async def _reconnect_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.25)
            session = self.session
            if session is not None and session.connected:
                continue
            if not self._running:
                return
            delay = next_backoff(self.connect_attempts)
            logger.warning("FlexRadio link lost — reconnecting in %.0fs", delay)
            self.connected = False
            await self._stop_tx_task()
            self.tx_active = False
            if session is not None:
                await session.close()
                self.session = None
            await asyncio.sleep(delay)
            if not self._running:
                return
            try:
                await self._connect_once()
            except (OSError, SessionError) as exc:
                logger.warning("FlexRadio reconnect failed: %s", exc)

    # ── radio setup ──────────────────────────────────────────────

    async def _probe_radio(self) -> None:
        session = self.session
        if session is None:
            return
        try:
            reply = await session.send("info")
        except SessionError:
            return
        info = protocol.parse_info(reply.body)
        self.model = info.get("model", "") or self.model
        self.serial = info.get("chassis_serial", "") or info.get("serial", "")
        self.software_version = info.get("software_ver", "")
        logger.info("FlexRadio: model=%s serial=%s firmware=%s protocol=%s",
                    self.model or "?", self.serial or "?", self.software_version or "?",
                    session.version or "?")

    async def ensure_pan(self) -> bool:
        """Create the panadapter/waterfall pair and set our aperture."""
        session = self.session
        if session is None:
            return False
        if not self.pan_id:
            try:
                reply = await session.send("display panafall create x=100 y=100")
            except SessionError:
                return False
            if not reply.ok:
                # `create` is refused when the radio's panadapter slots are all
                # taken (max_panadapters — another GUI client's, typically).
                # `_first_int` on an *error* body then hands back whatever number
                # it finds: measured on the field radio 2026-11-16, an error reply
                # became pan_id 0x40000000, i.e. the other operator's panadapter,
                # so every later `display pan set` retuned *his* waterfall.
                mine = self._own_pan(session)
                if mine is None:
                    logger.warning(
                        "FlexRadio: cannot create a panadapter (%s) and none of "
                        "the radio's %d belong to us — close a SmartSDR client or "
                        "free a panadapter slot", protocol.error_name(reply.code),
                        len(self.pans))
                    return False
                self.pan_id = mine
                logger.info("FlexRadio reusing our panadapter 0x%08X", mine)
            else:
                self.pan_id = _first_int(reply.body) or _PAN_ID_FALLBACK
                logger.info("FlexRadio panadapter id 0x%08X", self.pan_id)
        bandwidth = _mhz(self.bandwidth_hz)
        await session.send_ok(
            f"display pan set {self.pan_id} xpixels={XPIXELS} ypixels={YPIXELS} "
            f"min_dbm={vita.PAN_MIN_DBM:.1f} max_dbm={vita.PAN_MAX_DBM:.1f} "
            f"bandwidth={bandwidth} fps={self.fps}")
        return True

    def _own_pan(self, session) -> Optional[int]:
        """A panadapter that is ours to use when `create` is refused."""
        for pan_id, entry in sorted(self.pans.items()):
            handle = entry.get("client_handle")
            if not handle or not session.handle:
                continue
            try:
                if int(handle, 16) == session.handle:
                    return pan_id
            except (TypeError, ValueError):
                continue
        return None

    async def ensure_slice(self) -> bool:
        """Make sure a slice on **our own** panadapter is the one we drive.

        Field 2026-11-16 (FLEX-6600, box `ba1rb`): this engine never created a
        slice, so `own_slice` could only become set by finding one that carries
        our own ``client_handle`` — and on a radio where another client creates
        the slices (SmartSDR-Win does that automatically on any new pan), none
        ever does.  ``_slice_id()`` then fell back to ``min(slices)``, i.e. **the
        other operator's slice**: tuning wrote his frequency, the antenna command
        would have switched his connector, and our own panadapter showed a band
        nobody was listening to.  That is exactly how a box can look connected,
        display a waterfall and deliver no signal.

        Resolution order, most reliable first:

        1. a slice that already carries our handle (it is ours by definition);
        2. the **active** slice on our pan (the one the radio is demodulating);
        3. any slice on our pan — and activate it, because an inactive slice
           produces no audio;
        4. otherwise create one on our pan with ``slice create pan=<id>``.
        """
        session = self.session
        if session is None or not self.pan_id:
            return False
        try:
            await session.send("slice list")
        except SessionError:
            return False
        # `sub slice all` has already delivered every slice's status by now (the
        # handshake waits for its replies), so the choice below is local.
        ours = [(index, state) for index, state in sorted(self.slices.items())
                if state.get("pan_id") == self.pan_id]
        own_handle = None
        for index, state in sorted(self.slices.items()):
            if state.get("client_handle") and session.handle:
                try:
                    if int(state["client_handle"], 16) == session.handle:
                        own_handle = index
                        break
                except (TypeError, ValueError):
                    pass
        chosen = own_handle
        if chosen is None and ours:
            active = [index for index, state in ours if state.get("active")]
            chosen = (active or [index for index, _ in ours])[0]
        if chosen is None:
            try:
                reply = await session.send(f"slice create pan={self.pan_id}")
            except SessionError:
                return False
            chosen = _first_int(reply.body)
            if chosen is None:
                logger.warning("FlexRadio: slice create was answered with %r",
                               reply.body)
                return False
        self.own_slice = chosen
        # An inactive slice is silent; asking for it is one command and it is the
        # difference between "the box is broken" and "the box hears".
        state = self.slices.setdefault(chosen, {})
        if not state.get("active"):
            try:
                await session.send_ok(f"slice set {chosen} active=1")
                state["active"] = 1
            except SessionError:
                pass
        logger.info("FlexRadio slice %d on pan 0x%08X (%s)", chosen, self.pan_id,
                    "ours" if own_handle is not None else "adopted")
        return True

    async def ensure_streams(self) -> bool:
        session = self.session
        if session is None:
            return False
        if not self.rx_stream_ready:
            try:
                reply = await session.send(
                    "stream create type=remote_audio_rx compression=none")
            except SessionError:
                return False
            self.rx_stream_id = _first_int(reply.body) or _RX_STREAM_FALLBACK
            self.rx_stream_ready = reply.ok
            logger.info("FlexRadio RX audio stream 0x%08X (%s)",
                        self.rx_stream_id, "ok" if reply.ok else
                        protocol.error_name(reply.code))
        if not self.tx_stream_ready:
            try:
                reply = await session.send("stream create type=remote_audio_tx")
            except SessionError:
                return False
            self.tx_stream_id = _first_int(reply.body) or _TX_STREAM_FALLBACK
            self.tx_stream_ready = reply.ok
            logger.info("FlexRadio TX audio stream 0x%08X (%s) — the radio "
                        "forces Opus on this stream",
                        self.tx_stream_id, "ok" if reply.ok else
                        protocol.error_name(reply.code))
        return self.rx_stream_ready and self.tx_stream_ready

    async def set_bandwidth(self, hz: int) -> bool:
        session = self.session
        if session is None or not self.pan_id:
            return False
        self.bandwidth_hz = int(hz)
        return await session.send_ok(
            f"display pan set {self.pan_id} bandwidth={_mhz(hz)}")

    # ── status ───────────────────────────────────────────────────

    def _handle_status(self, status: Status) -> None:
        obj = status.obj
        if obj.startswith("slice "):
            self._update_slice(obj, status)
        elif obj.startswith("display pan "):
            self._update_pan(status, obj)
        elif obj.startswith("display waterfall "):
            self.waterfall_id = _first_int(obj) or self.waterfall_id
        elif obj.startswith("atu"):
            self.has_atu = True
        elif obj == "meter":
            defs = protocol.parse_meter_definitions(protocol.meter_text(status))
            if defs:
                self.meter_defs.update(defs)
        elif obj == "radio":
            self._update_radio(status)
        elif obj == "transmit":
            timeout = status.number("timeout")
            if timeout is not None:
                self.tx_timeout_s = timeout

    def _update_slice(self, obj: str, status: Status) -> None:
        index = _first_int(obj.split()[-1])
        if index is None:
            return
        state = self.slices.setdefault(index, {})
        freq = status.number("RF_frequency")
        if freq is not None:
            state["freq_hz"] = int(round(freq * 1e6))
            if index == self.own_slice and self._on_broadcast is not None:
                self._on_broadcast("freq_hz", state["freq_hz"])
        mode = status.get("mode")
        if mode:
            changed = state.get("mode") != mode
            state["mode"] = mode
            if changed and index == self.own_slice and self._on_broadcast is not None:
                self._on_broadcast("mode", mode)
        for key, field in (("filter_lo", "filter_lo"), ("filter_hi", "filter_hi"),
                           ("audio_level", "audio_level")):
            value = status.number(key)
            if value is not None:
                state[field] = int(round(value))
        # Antenna selection.  `rxant` is the one that decides whether the slice
        # hears anything at all, and `ant_list` is the radio's own spelling of
        # what it offers — read them, never hardcode them (measured on a
        # FLEX-6600, 2026-11-16: ANT1,ANT2,RX_A,RX_B,XVTA,XVTB).  The old code
        # sent `ant=ANT1`, a key this firmware does not have, so the command was
        # accepted and changed nothing.
        rxant = status.get("rxant")
        if rxant:
            changed = state.get("rxant") != rxant
            state["rxant"] = rxant
            if changed and index == self.own_slice and self._on_broadcast is not None:
                self._on_broadcast("antenna_name", rxant)
        txant = status.get("txant")
        if txant:
            state["txant"] = txant
        ant_list = status.get("ant_list")
        if ant_list:
            names = [name for name in ant_list.split(",") if name]
            if names != state.get("antenna_list"):
                state["antenna_list"] = names
                if index == self.own_slice and self._on_broadcast is not None:
                    self._on_broadcast("antenna_list", names)
        owner = status.get("client_handle")
        if owner:
            # Kept verbatim (hex string) so a reconnect can still tell whose slice
            # it is; `ensure_slice()` compares it against the session's handle.
            state["client_handle"] = owner
        pan = status.get("pan")
        if pan:
            pan_id = _first_int(pan)
            if pan_id is not None:
                state["pan_id"] = pan_id
        active = status.number("active")
        if active is not None:
            state["active"] = int(round(active))
        session = self.session
        if owner and session is not None and self.own_slice is None:
            try:
                if int(owner, 16) == session.handle:
                    self.own_slice = index
            except ValueError:
                pass

    def _update_pan(self, status: Status, obj: str = "") -> None:
        bandwidth = status.number("bandwidth")
        if bandwidth is not None and bandwidth > 0:
            self.bandwidth_hz = int(round(bandwidth * 1e6))
        pan_id = _first_int(obj.split()[-1]) if obj else None
        if pan_id is not None:
            entry = self.pans.setdefault(pan_id, {})
            handle = status.get("client_handle")
            if handle:
                entry["client_handle"] = handle
            if not entry.get("created"):
                entry["created"] = True
        center = status.number("center")
        if center is not None:
            self.pan_center_hz = int(round(center * 1e6))

    def _update_radio(self, status: Status) -> None:
        model = status.get("model")
        if model:
            self.model = model
        serial = status.get("serial")
        if serial:
            self.serial = serial

    def _handle_interlock(self, transition) -> None:
        if self._on_broadcast is None:
            return
        if transition.state not in ("TRANSMITTING", "READY", "RECEIVE",
                                    "TX_FAULT", "TIMEOUT"):
            return
        self._on_broadcast("tx_status",
                           transition.state == "TRANSMITTING")

    # ── packets ──────────────────────────────────────────────────

    def _handle_packet(self, data: bytes) -> None:
        try:
            header = vita.parse_header(data)
        except vita.VitaError:
            return
        if header.pcc == vita.PCC_FFT:
            # The radio sends the FFT of **every** panadapter we subscribed to,
            # addressed by stream id = pan id.  A second client (SmartSDR-Win on
            # the owner's PC — the normal case on a shared radio) has its own
            # pans, and their segments used to be fed to the same 850-bin
            # assembler: measured 2026-11-16, 238 of our pan's segments and 198
            # of a foreign pan's in 8 s.  Halves the frames and mixes bins from
            # two different bandwidths into one picture.
            if self.pan_id and header.stream_id != self.pan_id:
                self.foreign_packets += 1
                return
            self._ingest_fft(header.payload)
        elif header.pcc == vita.PCC_RX_AUDIO:
            # Same reasoning as the FFT: an audio datagram that is not our own
            # RX stream (the id `stream create` handed us) must not enter the
            # resampler.
            if self.rx_stream_id and header.stream_id != self.rx_stream_id:
                self.foreign_packets += 1
                return
            self._ingest_audio(header.payload)
        elif header.pcc == vita.PCC_METERS:
            try:
                self.meter_values.update(vita.parse_meters(header.payload))
            except vita.VitaError:
                pass

    def _ingest_fft(self, payload: bytes) -> None:
        try:
            segment = vita.parse_fft_segment(payload)
            frame = self._assembler.feed(segment)
        except vita.VitaError:
            self.spectrum_errors += 1
            return
        if frame is None:
            return
        if frame.size != XPIXELS:
            frame = _resample_bins(frame, XPIXELS)
        self._spectrum = vita.pixels_to_ui(frame)
        self.spectrum_frames += 1

    def _ingest_audio(self, payload: bytes) -> None:
        try:
            frames = vita.parse_audio(payload)
        except vita.VitaError:
            return
        mono = vita.audio_mono(frames)
        if mono.size == 0:
            return
        self.audio_packets += 1
        self.rx_audio.append(mono)
        self.rx_samples += mono.size
        limit = int(RX_SAMPLE_RATE * RX_QUEUE_SECONDS)
        while self.rx_samples > limit and self.rx_audio:
            dropped = self.rx_audio.popleft()
            self.rx_samples -= dropped.size
            self.rx_drops += 1

    # ── readers used by audio/scope/backend ──────────────────────

    def spectrum_bins(self) -> Optional[List[int]]:
        """Latest 850-bin frame as UI bytes, or None before the first one."""
        spectrum = self._spectrum
        if spectrum is None:
            return None
        return spectrum.tolist()

    def slice_state(self) -> Dict[str, Any]:
        index = self.own_slice
        if index is None:
            return {}
        return dict(self.slices.get(index, {}))

    def meter_by_name(self, src: str, name: str) -> Optional[float]:
        """Scaled value of the meter called ``name`` from source ``src``."""
        for definition in self.meter_defs.values():
            if definition.src == src and definition.name == name:
                raw = self.meter_values.get(definition.meter_id)
                if raw is None:
                    return None
                return protocol.scale_meter(raw, definition.unit)
        return None

    def s_meter_dbm(self) -> Optional[float]:
        return self.meter_by_name("SLC", "LEVEL")

    def forward_power_watts(self) -> Optional[float]:
        dbm = self.meter_by_name("TX-", "FWDPWR")
        if dbm is None:
            return None
        return 10.0 ** (dbm / 10.0) / 1000.0

    # ── transmit ─────────────────────────────────────────────────

    def feed_opus(self, packet: bytes) -> None:
        """Queue one ready-to-send VITA Opus packet for the pacer."""
        if len(self._tx_queue) >= TX_QUEUE_PACKETS:
            self._tx_queue.popleft()
            self.tx_drops += 1
        self._tx_queue.append(packet)

    async def start_tx(self) -> None:
        self.tx_active = True
        self._tx_deadline = time.monotonic()
        if self._tx_task is None or self._tx_task.done():
            self._tx_task = asyncio.create_task(self._tx_loop(), name="flex-tx")

    async def stop_tx(self, graceful: bool = False) -> None:
        self.tx_active = False
        if graceful:
            deadline = time.monotonic() + 0.25
            while self._tx_queue and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
        self._tx_queue.clear()
        await self._stop_tx_task()

    async def _stop_tx_task(self) -> None:
        task = self._tx_task
        self._tx_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass

    async def _tx_loop(self) -> None:
        session = self.session
        while True:
            await asyncio.sleep(TX_PACKET_INTERVAL_S / 2)
            if not self.tx_active or session is None or not session.connected:
                self._tx_deadline = time.monotonic()
                continue
            now = time.monotonic()
            if now < self._tx_deadline:
                continue
            if not self._tx_queue:
                self.tx_underruns += 1
                self._tx_deadline = now + TX_PACKET_INTERVAL_S
                continue
            packet = self._tx_queue.popleft()
            try:
                sock = session._udp           # the session owns the socket
                if sock is not None:
                    # Raw socket method, not ``loop.sock_sendto`` (uvloop raises
                    # NotImplementedError for that one) — and the address is
                    # explicit because the socket is not connected: a FLEX-6600
                    # streams from UDP 4993, so connecting would filter it out
                    # (both measured in the field, 2026-11-16).
                    sock.sendto(packet, (session.host, session.port))
                    self.tx_packets_sent += 1
            except (OSError, RuntimeError) as exc:
                logger.debug("FlexRadio TX packet send failed: %s", exc)
                self.tx_underruns += 1
            self._tx_deadline += TX_PACKET_INTERVAL_S
            if now - self._tx_deadline > 0.2:      # fell far behind: resync
                self._tx_deadline = now + TX_PACKET_INTERVAL_S

    async def key(self, on: bool) -> bool:
        """Key/unkey through the interlock state machine (design §7.2)."""
        session = self.session
        if session is None:
            return False
        if not on:
            await self.stop_tx()
            try:
                return await session.send_ok("xmit 0")
            except SessionError:
                return False
        if not await session.send_ok("xmit 1"):
            return False
        ok = await session.wait_interlock({"TRANSMITTING"}, XMIT_CONFIRM_S)
        if not ok:
            logger.warning("FlexRadio key-up did not reach TRANSMITTING "
                           "(interlock=%s) — unkeying",
                           session.interlock_state or "unknown")
            await session.send_ok("xmit 0")
            await self.stop_tx()
            return False
        return True

    async def tune(self, on: bool) -> bool:
        session = self.session
        if session is None:
            return False
        return await session.send_ok(f"transmit tune {'on' if on else 'off'}")

    def set_broadcast_callback(
            self, cb: Optional[Callable[[str, Any], None]]) -> None:
        """Register the sink for radio-originated changes (freq/mode/PTT)."""
        self._on_broadcast = cb

    # ── diagnostics ──────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "model": self.model,
            "serial": self.serial,
            "firmware": self.software_version,
            "connected": self.connected,
            "pan_id": self.pan_id,
            "rx_stream_id": self.rx_stream_id,
            "tx_stream_id": self.tx_stream_id,
            "spectrum_frames": self.spectrum_frames,
            "spectrum_errors": self.spectrum_errors,
            "audio_packets": self.audio_packets,
            "rx_drops": self.rx_drops,
            "tx_packets_sent": self.tx_packets_sent,
            "tx_drops": self.tx_drops,
            "tx_underruns": self.tx_underruns,
            "meters": len(self.meter_defs),
            "has_atu": self.has_atu,
            "tx_timeout_s": self.tx_timeout_s,
        }


def _resample_bins(bins: np.ndarray, count: int) -> np.ndarray:
    """Linear resample of a pixel row to ``count`` entries.

    Only used when the radio refuses ``xpixels=850``; the mapping stays in the
    pixel domain, so the dBm aperture is unaffected (design §6, U3).
    """
    if bins.size == 0:
        return np.zeros(count, dtype=np.uint16)
    if bins.size == count:
        return bins
    src = np.linspace(0, bins.size - 1, count)
    resampled = np.interp(src, np.arange(bins.size), bins.astype(np.float64))
    return np.clip(np.rint(resampled), 0, 65535).astype(np.uint16)
