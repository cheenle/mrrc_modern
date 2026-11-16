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

import config

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
        #: 电台回报的 pan 孔径（权威）。我们请求的值可能被夹住，所以像素→dBm
        #: 一律用它；空字典 = 还没收到 pan 状态（退回常量）。
        self.pan_aperture: dict = {}
        self._pan_status_dumped = False
        self._raw_frame_probe = 0
        self.fft_total_bins: Optional[int] = None
        self.fft_foreign_rows = 0
        self._segment_probe = 0
        self.spectrum_overrange = 0
        self.spectrum_gap_bins = 0
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
        #: last antenna snapshot broadcast, so `_emit_antennas()` only speaks on change
        self._last_antennas: Dict[str, Any] = {}

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
        if not await self.ensure_pan():
            # Half a session is worse than none: without a panadapter there is no
            # spectrum to show, and `_slice_id()` would fall back to whatever slice
            # the radio lists first — another operator's (see `ensure_slice`).
            logger.warning("FlexRadio: no panadapter available, not serving this "
                           "radio until one frees up")
            await session.close()
            return False
        await self.ensure_slice()
        await self._restore_antenna()
        await self.ensure_streams()
        await self._refresh_object_status()
        return True

    async def _refresh_object_status(self) -> None:
        """Re-subscribe so the radio sends the **full** status of our objects.

        `session._handshake()` subscribes before `ensure_pan()` creates anything,
        so the only status we ever saw for our own panadapter was a delta
        (measured 2026-11-16: a `display pan` line carrying nothing but
        `noise_floor_*`).  That left `x_pixels`, `y_pixels`, `bandwidth` and the
        aperture as *assumptions* — and the spectrum's calibration rests on them.
        Re-issuing the subscription makes the radio re-announce existing objects.
        """
        session = self.session
        if session is None:
            return
        self._pan_status_dumped = False       # log the full status this time
        for topic in ("pan", "slice"):
            try:
                await session.send(f"sub {topic} all")
            except SessionError:
                return
        await asyncio.sleep(0.2)

    def pan_geometry(self) -> Dict[str, Any]:
        """What the radio says about the pan our spectrum comes from.

        Every field is what the **radio** reported (or what we set and it
        confirmed); `None` means "never observed", never a made-up default.
        """
        aperture = dict(self.pan_aperture)
        return {
            "pan_id": f"0x{self.pan_id:08X}" if self.pan_id else None,
            "center_hz": self.pan_center_hz or None,
            "bandwidth_hz": self.bandwidth_hz or None,
            "min_dbm": aperture.get("min_dbm"),
            "max_dbm": aperture.get("max_dbm"),
            "x_pixels": aperture.get("xpixels"),
            "y_pixels": aperture.get("ypixels"),
            "fps": aperture.get("fps"),
            "fft_bins": int(self._spectrum.size) if self._spectrum is not None else None,
            "segment_total_bins": self.fft_total_bins,
        }

    async def _restore_antenna(self) -> None:
        """Put the operator's RX antenna back after a reconnect.

        实测（FLEX-6600，2026-11-16）：电台**不保留** slice 的 `rxant` —— 我们每次
        重连它都把 slice 0 放回 `ANT1`，而操作员的天线在 `ANT2`。表现就是盒子"有
        声音"（其实是失去天线后的底噪）而瀑布里一个信号都没有。界面上的选择记在
        `MRRC_FLEX_RX_ANT`，连上之后在这里重新施加。
        """
        session = self.session
        wanted = str(getattr(config, "FLEX_RX_ANT", "") or "").strip().upper()
        if session is None or not wanted or self.own_slice is None:
            return
        state = self.slices.get(self.own_slice) or {}
        options = state.get("antenna_list") or []
        if options and wanted not in options:
            logger.warning("FlexRadio: saved RX antenna %r is not on this radio's "
                           "list (%s) — leaving the radio's own choice alone",
                           wanted, ", ".join(options))
            return
        current = state.get("rxant")
        if current == wanted:
            return
        try:
            await session.send_ok(f"slice set {self.own_slice} rxant={wanted}")
        except SessionError:
            return
        state["rxant"] = wanted
        logger.info("FlexRadio: RX antenna %s → %s (the radio had reset it to %s)",
                    current or "?", wanted, current or "?")

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
            await self._release_radio_resources(session)
            await session.close()
        self.rx_audio.clear()
        self.rx_samples = 0

    async def _release_radio_resources(self, session) -> None:
        """Give the panadapter（和我们在上面建的 slice）back to the radio.

        Disconnecting a TCP client does **not** reliably reclaim them:
        measured 2026-11-16 after the crash loop (98 restarts), the radio reported
        ``available_panadapters=0`` and ``available_slices=0`` for a FLEX-6600
        whose only real users were two GUI clients — so the next session could not
        create a panadapter at all (`0x50000009`).  Every reconnect therefore
        leaked one panadapter.

        The slice is removed only when its ``client_handle`` is ours; a slice the
        radio's other client created on our pan is not ours to delete.
        """
        state = self.slices.get(self.own_slice) if self.own_slice is not None else None
        handle = getattr(session, "handle", None)
        try:
            ours = bool(state) and handle is not None and \
                int(str(state.get("client_handle", "")), 16) == handle
        except (TypeError, ValueError):
            ours = False          # 解析不了就当不是我们的：宁可留下，也不要删别人的
        if ours:
            try:
                await session.send_ok(f"slice remove {self.own_slice}")
            except SessionError:
                pass
            self.own_slice = None
        if self.pan_id:
            try:
                await session.send_ok(f"display panafall remove {self.pan_id}")
            except SessionError:
                pass
            self.pan_id = 0
            self.waterfall_id = 0

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
        # **pan 跟着 slice 走，不是反过来。**
        # Flex 的 panadapter 只有在**挂着 slice**时才真正接上天线（DDC 由 slice
        # 决定）；一块空 pan 照样会送 FFT，但那是没有射频输入的底噪。2026-11-16
        # 现场：重启后 `display panafall create` 给了新 pan 0x40000001，而我们
        # 的 slice 0 还挂在上一轮创建的 0x40000000 上 —— 于是声音（slice 0 →
        # ANT1）正常，瀑布（0x40000001）一片平底噪、换任何天线都是 0.05 dB 的
        # 变化。所以先看 slice 在哪块 pan 上，在那儿就地取用。
        if not self.pan_id:
            mine = self._slice_pan()
            if mine is not None:
                self.pan_id = mine
                logger.info(
                    "FlexRadio: our slice is already on panadapter 0x%08X — "
                    "displaying that one instead of creating another", mine)
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
        await self._apply_pan_display()
        return True

    async def _apply_pan_display(self) -> None:
        """Write the display parameters (span, aperture, fps) to `self.pan_id`."""
        session = self.session
        if session is None or not self.pan_id:
            return
        await session.send_ok(
            f"display pan set {self.pan_id} xpixels={XPIXELS} ypixels={YPIXELS} "
            f"min_dbm={vita.PAN_MIN_DBM:.1f} max_dbm={vita.PAN_MAX_DBM:.1f} "
            f"bandwidth={_mhz(self.bandwidth_hz)} fps={self.fps}")

    def _slice_pan(self) -> Optional[int]:
        """The panadapter our own slice sits on, if the radio has told us.

        `slice list` / `sub slice all` carry `pan=0x…` per slice, so the mapping
        slice → pan is radio-authoritative.  This is the pan whose spectrum is
        wired to the antenna through that slice; a freshly created empty pan is
        not.
        """
        candidates = []
        if self.own_slice is not None:
            candidates.append(self.own_slice)
        candidates.extend(index for index in sorted(self.slices) if index not in candidates)
        for index in candidates:
            state = self.slices.get(index) or {}
            pan = state.get("pan_id")
            if not pan:
                continue
            handle = state.get("client_handle")
            if self.own_slice is not None and index == self.own_slice:
                return int(pan)
            if handle and self.session is not None and self.session.handle:
                try:
                    if int(handle, 16) == self.session.handle:
                        return int(pan)
                except (TypeError, ValueError):
                    continue
        return None

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
        if session is None:
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
        # **pan 跟着 slice 走。** 选定的 slice 可能挂在我们**上一轮**创建的 pan 上
        # （重启后 `display panafall create` 会给一块新 pan，而 slice 还留在旧的
        # 那块）。显示必须在 slice 所在的 pan 上，否则我们渲染的是一块没有射频
        # 输入的空 pan：换任何天线都是 0.05 dB 的底噪（2026-11-16 实测）。
        slice_pan = (self.slices.get(chosen) or {}).get("pan_id")
        if slice_pan and int(slice_pan) != (self.pan_id or 0):
            logger.info(
                "FlexRadio: slice %d lives on panadapter 0x%08X — moving the "
                "display there from 0x%08X", chosen, int(slice_pan),
                self.pan_id or 0)
            self.pan_id = int(slice_pan)
            self.pan_center_hz = 0
            self.pan_aperture = {}
            try:
                await self._apply_pan_display()
            except SessionError:
                pass
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
        # 接收增益链：`rfgain`（SCU×天线 的射频增益）、`agc_threshold`（AGC-T：
        # 能施加的最大增益）、`agc_mode`（速度）。按照厂商的调法，这三样才决定
        # 「信号强不强」——带宽和天线只决定「能不能收到」（2026-11-16 现场：
        # LSB 下信号微弱，电台自带的值从来没被我们读过，更谈不上调）。
        rfgain = status.number("rfgain")
        if rfgain is not None:
            changed = state.get("rf_gain") != rfgain
            state["rf_gain"] = rfgain
            if changed and index == self.own_slice and self._on_broadcast is not None:
                self._on_broadcast("rf_gain_raw", rfgain)
        agc_t = status.number("agc_threshold")
        if agc_t is not None:
            changed = state.get("agc_threshold") != agc_t
            state["agc_threshold"] = int(round(agc_t))
            if changed and index == self.own_slice and self._on_broadcast is not None:
                self._on_broadcast("agc_threshold", int(round(agc_t)))
        agc_mode = status.get("agc_mode")
        if agc_mode:
            state["agc_mode"] = agc_mode
        agc_t_mode = status.get("agc_threshold_mode")
        if agc_t_mode:
            state["agc_threshold_mode"] = agc_t_mode
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
        tx_list = status.get("tx_ant_list")
        if tx_list:
            state["tx_ant_list"] = [name for name in tx_list.split(",") if name]
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
        self._emit_antennas()

    def antennas_snapshot(self) -> Dict[str, Any]:
        """Everything the antenna panel needs, in one JSON-safe object.

        Grouped by **panadapter** because that is the scope the radio enforces:
        "All Slices placed on a Panadapter must use the same receive antenna as
        the Panadapter" — so an RX selector belongs to a pan, not to a slice,
        while the TX antenna really is per slice.  Names come from the radio
        (`ant_list` / `tx_ant_list`); nothing here is hardcoded per model.
        """
        own = self.slices.get(self.own_slice) if self.own_slice is not None else {}
        own_pan = (own or {}).get("pan_id")
        pans: Dict[int, Dict[str, Any]] = {}
        for index, state in sorted(self.slices.items()):
            pan = state.get("pan_id")
            if pan is None:
                continue
            entry = pans.setdefault(pan, {
                "id": f"0x{pan:08X}",
                "ours": pan == own_pan,
                "rxant": state.get("rxant") or "",
                "slices": [],
            })
            entry["slices"].append({
                "index": index,
                "freq_hz": state.get("freq_hz") or 0,
                "mode": state.get("mode") or "",
                "active": bool(state.get("active")),
                "ours": index == self.own_slice,
                "txant": state.get("txant") or "",
            })
        return {
            "rx": list((own or {}).get("antenna_list") or []),
            "tx": list((own or {}).get("tx_ant_list") or []),
            "pans": [pans[key] for key in sorted(pans)],
        }

    def _emit_antennas(self) -> None:
        """Broadcast the antenna snapshot when it actually changed."""
        if self._on_broadcast is None:
            return
        snapshot = self.antennas_snapshot()
        if snapshot == self._last_antennas:
            return
        self._last_antennas = snapshot
        self._on_broadcast("antennas", snapshot)

    def _update_pan(self, status: Status, obj: str = "") -> None:
        # **只有我们自己的 pan 才能改我们的状态。**
        # 我们订阅的是 `sub pan all` —— 电台会把**每一个客户端**的 pan 状态都
        # 推过来（同机另一台 SmartSDR-Win 是常态）。原先这里不分对象地写
        # `bandwidth_hz` / `pan_center_hz`，于是：
        #   · `ensure_pan_covers_vfo()` 按**别人的**中心判断我们该不该重定位；
        #   · `scope_start_freq`（频谱横轴）用**别人的**中心与带宽；
        #   · 像素→dBm 用**别人的**孔径（实测 3 ms 内在 -140/-50 与 -135/-40
        #     之间来回跳）。
        # 三者叠加的表现就是 2026-11-16 的现场：声音正常、频谱里找不到信号，
        # 因为画面被画到了窗外（同一个 pan id 的两套状态在抢）。FFT **数据**本身
        # 早就在 FP55 按 stream id 过滤过，所以一直是我们的 —— 歪的是坐标与标定。
        pan_id = _first_int(obj.split()[-1]) if obj else None
        mine = pan_id is not None and self.pan_id is not None and pan_id == self.pan_id
        if pan_id is not None and self.pan_id is None:
            # 还没 create/没解析出自己的 id 之前，任何 pan 的状态都不可用。
            mine = False
        bandwidth = status.number("bandwidth")
        if mine and bandwidth is not None and bandwidth > 0:
            self.bandwidth_hz = int(round(bandwidth * 1e6))
        # **孔径是电台的，不是我们的。** 我们发送 `min_dbm/max_dbm/ypixels`，
        # 但电台有权夹住它们（不合法时回 `0x50000004` 取值越界），而且它的
        # 默认值与请求值可以不同。像素→dBm 只有用**它回报的**孔径才正确；
        # 用请求值去算，整个瀑布就带着一个未知的 dB 偏移 —— 这正是
        # 2026-11-16 连续四次"调窗口还是黑"的真正原因（每次调的都是我们
        # 假设的孔径，而电台用的是它自己的）。
        aperture = {}
        if mine and not self._pan_status_dumped:
            self._pan_status_dumped = True
            logger.info("FlexRadio pan status (raw, once, %s): %s", obj,
                        dict(status.kv))
        for key in ("min_dbm", "max_dbm", "ypixels", "y_pixels",
                    "xpixels", "x_pixels", "fps"):
            value = status.number(key)
            if value is not None:
                aperture[key] = value
        if "y_pixels" in aperture:
            aperture.setdefault("ypixels", aperture.pop("y_pixels"))
        if "x_pixels" in aperture:
            aperture.setdefault("xpixels", aperture.pop("x_pixels"))
        if aperture and not mine:
            aperture = {}
        if aperture:
            before = dict(self.pan_aperture)
            self.pan_aperture.update(aperture)
            if self.pan_aperture != before:
                logger.info(
                    "FlexRadio pan %s aperture from the radio: "
                    "min_dbm=%.1f max_dbm=%.1f ypixels=%d xpixels=%d fps=%.1f",
                    obj or "?",
                    self.pan_aperture.get("min_dbm", float("nan")),
                    self.pan_aperture.get("max_dbm", float("nan")),
                    int(self.pan_aperture.get("ypixels", 0)),
                    int(self.pan_aperture.get("xpixels", 0)),
                    self.pan_aperture.get("fps", 0.0))
        pan_id = _first_int(obj.split()[-1]) if obj else None
        if pan_id is not None:
            entry = self.pans.setdefault(pan_id, {})
            handle = status.get("client_handle")
            if handle:
                entry["client_handle"] = handle
            if not entry.get("created"):
                entry["created"] = True
        center = status.number("center")
        if mine and center is not None:
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
            # 电台把一行 FFT 切成几段发（实测 4.3.16：670 + 180 = 850 桶，子头
            # 16 字节带 start_bin），所以必须按 start_bin 落位；`total_bins` 是
            # 整行宽度，用来确认拼出来的帧有多宽。
            self.fft_total_bins = int(segment.total_bins)
            # 电台在配置刚下发时还会推一批**粗行**（实测：`total_bins=50`、
            # `frame=0`，116 字节），随后才是真正的 670+180=850。粗行会被当成
            # "完整一帧"插值到全宽，于是画面在粗细之间来回跳。以回读的
            # `x_pixels` 为准，宽度对不上的分段一律不结算。
            width = int(self.pan_aperture.get("xpixels") or 0)
            if width and segment.total_bins != width:
                self.fft_foreign_rows += 1
                return
            if self._segment_probe < 3:
                self._segment_probe += 1
                logger.info(
                    "FlexRadio FFT segment: start_bin=%d num_bins=%d "
                    "total_bins=%d bin_size=%d frame=%d subheader=%dB",
                    segment.start_bin, segment.num_bins, segment.total_bins,
                    segment.bin_size, segment.frame_index,
                    segment.subheader_bytes)
            frame = self._assembler.feed(segment)
        except vita.VitaError:
            self.spectrum_errors += 1
            return
        if frame is None:
            return
        if frame.size != XPIXELS:
            frame = _resample_bins(frame, XPIXELS)
        # 极少数桶会报出 255 之外的值（实测 2026-11-16：噪底 174、真实信号 20，
        # 偶尔蹦出 543/888/905）。像素域是 0..255（`y_pixels` = 256，已由 pan 完整
        # 状态确认），超出量程没有含义；不钳位会被 `255 - pixel` 折成一个**假中灰**。
        # 每个分段末尾的零终止符（实测 668/669、848/849 恒为 0）先补掉：像素 0 在
        # 这里是"最大值"，不补就是两条固定的假谱线。
        if frame.dtype != np.uint16 or True:
            filled = vita.fill_short_zero_runs(frame)
            if filled is not frame:
                self.spectrum_gap_bins += int((filled != frame).sum()) if filled.size == frame.size else 0
                frame = filled
        over = int((frame > 255).sum())
        if over:
            self.spectrum_overrange += over
            frame = np.clip(frame, 0, 255)
        self._spectrum = (255 - frame).astype(np.uint8)
        self._spectrum_ready = True
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

    def meter_by_name(self, src: str, name: str,
                      num: Optional[int] = None) -> Optional[float]:
        """Scaled value of the meter called ``name`` from source ``src``.

        ``num`` selects *which* meter when the radio defines one per slice:
        a FLEX-6600 defines ``SLC / LEVEL`` for every slice it has
        (``num=0…3``), and taking the first one handed the UI another
        operator's S-meter (field 2026-11-16, box `ba1rb`: changing our own
        slice's antenna could never move a meter that belonged to slice 0).
        """
        for definition in self.meter_defs.values():
            if definition.src != src or definition.name != name:
                continue
            if num is not None and definition.num != num:
                continue
            raw = self.meter_values.get(definition.meter_id)
            if raw is None:
                return None
            return protocol.scale_meter(raw, definition.unit)
        return None

    def s_meter_dbm(self) -> Optional[float]:
        """Our own slice's level, never the first one the radio happens to list."""
        if self.own_slice is not None:
            dbm = self.meter_by_name("SLC", "LEVEL", num=self.own_slice)
            if dbm is not None:
                return dbm
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
