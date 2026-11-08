"""SunSDR2 DX — single-asyncio UDP device engine (native backend, Task 3).

One asyncio engine owns the whole device session, replacing the reference
project's split of UDP work across ``sunmrrc/server.py`` globals, a
blocking-socket keep-alive thread and a ``time.sleep()`` TX pacer thread:

- BOTH UDP sockets (control 50001, IQ 50002) are bound before the boot
  sequence is sent — a failed second bind cleans up the half-open first
  socket and ``start()`` returns False with a clear log.  Empty host is
  refused the same way.  ``start()`` is idempotent; ``stop()`` repeatable.
- The captured boot sequence comes from Task 1's
  :func:`backends.sunsdr.protocol.build_boot_sequence`, paced at
  ``boot_spacing_s`` (30 ms in production, 0 in tests).
- A 0.5 s control heartbeat (0x0018) and an 0xFFFE IQ keep-alive run as
  plain asyncio tasks — they never touch the DSP, so a busy demodulator
  can delay them by a scheduling quantum at worst, never stall them
  structurally.  Both share one stream counter with the TX pacer; every
  increment is masked ``& 0xFFFFFFFF`` (PROTOCOL.md §17.2, and the
  counter resets to 0x04B0 at each PTT assert, as the real client does).
- RX: stream datagrams go through Task 1's :func:`parse_rx_packet`; IQ
  blocks are demodulated in-loop by Task 2's :class:`AudioDemodulator`
  (200-sample packets are small-vector work) into a bounded deque of 48 kHz
  int16 audio that stays alive with zero consumers — spectrum clients,
  recorders and FT8 taps are consumers, not lifelines.  The newest
  ``SPECTRUM_FFT_SIZE`` IQ samples are retained for the scope adapter;
  0x1F00 power telemetry is stored + broadcast; 0x1F01 confirms
  ``ptt_confirmed``; anything malformed is counted and dropped.
- TX: :meth:`feed_tx_audio` runs Task 2's :class:`TXModulator` and pushes
  1200-byte payloads into a bounded queue (full → drop oldest, counted).
  A dedicated pacer task writes 0xFFFD packets at
  ``IQ_SAMPLES_PER_PACKET / (sample_rate / 2)`` s — the measured device
  fact from PROTOCOL.md §17.3 is TX = RX / 2, so the pace follows the RX
  rate and the modulator is rebuilt when the rate changes.  An underrun
  emits one zero-IQ packet and is counted; a popped payload is never sent
  twice.  ``stop_tx()`` is immediate by default, or a bounded graceful
  drain of the queued audio.
- Control ordering mirrors ExpertSDR3: ``set_frequency`` → RX_FREQ,
  TX_FREQ, DRIVE (QSY re-sends the current drive); ``set_ptt(True)`` →
  DRIVE then PTT1; ``set_ptt(False)`` clears the request state and sends
  PTT0 with no blocking read-back, and works even if the DSP path or the
  transport is broken.  ``stop()`` sends the PTT release BEFORE closing
  the transports and does all of that before its first ``await``, so a
  cancelled ``stop()`` cannot skip the release.
- Sample rate: only the four protocol rates are accepted; a switch is
  refused while TX is active and otherwise rebuilds the demodulator +
  modulator inside a controlled single-flight re-boot.  Stream-recovery
  re-boots are rate-limited and can never overlap.

No imports from ``server``/``radio_state`` — the optional ``on_event``
callback is the only outward channel.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import socket
import struct
import time
from typing import Callable, Optional, Union

import numpy as np

from .dsp import (
    AUDIO_RATE,
    SPECTRUM_FFT_SIZE,
    AudioDemodulator,
    TXModulator,
    iq_to_spectrum,
)
from .protocol import (
    MAGIC,
    CmdID,
    IF_OFFSET_HZ,
    IQ_PAYLOAD_BYTES,
    IQ_SUBTYPE,
    SAMPLE_RATES,
    STREAM_COUNTER_START,
    STREAM_COUNTER_STEP,
    build_boot_sequence,
    build_drive,
    build_packet,
    build_ptt,
    build_rx_frequency,
    build_tx_frequency,
    drive_byte,
    parse_rx_packet,
)

logger = logging.getLogger(__name__)

DEFAULT_FREQ_HZ = 7_074_000.0
DEFAULT_DRIVE_PCT = 100
BOOT_SPACING_S = 0.03            # inter-packet spacing measured in the capture
# TX stream sub-type (PROTOCOL.md §17.2).  Task 1's protocol module only
# needed the RX sub-types; the TX frame header lives here at the send site.
TX_SUBTYPE = 0xFFFD
TX_FLAGS = 0x0102                # stream data frames (keep-alive uses 0x0001)
KEEPALIVE_FLAGS = 0x0001
DEFAULT_TX_QUEUE_MAX = 600       # ~3 s of audio at 195 pkt/s

# RX mode -> TX modulation.  DIGI-U is USB for transmit (FT8 and every
# other sound-card data mode); CW/AM/FM have no verified keying/modulator
# on this backend yet, so they are BLOCKED rather than silently inheriting
# the previous sideband (see _apply_tx_mode / set_ptt).
_TX_MODE_FOR_RX = {"USB": "USB", "LSB": "LSB", "DIGI": "USB"}


def _rate_key(rate: Union[str, int]) -> str:
    """Normalize a rate key (``"78k"``) or Hz (78125) to a protocol key."""
    if isinstance(rate, str):
        if rate in SAMPLE_RATES:
            return rate
        raise ValueError(
            f"unknown IQ sample rate {rate!r}; one of {sorted(SAMPLE_RATES)}")
    for key, hz in SAMPLE_RATES.items():
        if hz == rate:
            return key
    raise ValueError(
        f"unknown IQ sample rate {rate!r}; one of "
        f"{sorted(SAMPLE_RATES.values())}")


class _EngineDatagramProtocol(asyncio.DatagramProtocol):
    """Thin asyncio.Protocol that funnels datagrams into the engine."""

    def __init__(self, engine: "SunSDREngine", which: str):
        self._engine = engine
        self._which = which

    def datagram_received(self, data: bytes, addr):
        self._engine._on_datagram(self._which, data, addr)

    def error_received(self, exc):
        logger.warning("SunSDR engine: UDP error on %s socket: %s",
                       self._which, exc)

    def connection_lost(self, exc):
        if exc is not None:
            logger.warning("SunSDR engine: %s socket closed: %s",
                           self._which, exc)


class SunSDREngine:
    """Single-asyncio UDP engine for one SunSDR2 DX device session."""

    def __init__(self, host: str, local_host: str = "",
                 control_port: int = 50001, iq_port: int = 50002,
                 sample_rate: Union[str, int] = 78125,
                 heartbeat_s: float = 0.5, *,
                 bind_control_port: Optional[int] = None,
                 bind_iq_port: Optional[int] = None,
                 boot_spacing_s: float = BOOT_SPACING_S,
                 tx_queue_max: int = DEFAULT_TX_QUEUE_MAX,
                 tx_interval_s: Optional[float] = None,
                 tx_prime_packets: int = 60,
                 tx_reprime_packets: int = 8,
                 recovery_idle_s: float = 10.0,
                 recovery_cooldown_s: float = 5.0,
                 rx_audio_max: int = 1024,
                 rx_demod_block: int = 3125,
                 sent_log_max: int = 512,
                 ptt_release_retries: int = 2,
                 ptt_release_retry_s: float = 0.25,
                 ptt_confirm_timeout_s: float = 2.0,
                 ptt_confirm_retries: int = 1,
                 on_event: Optional[Callable[[str, dict], None]] = None):
        self.host = str(host).strip() if host else ""
        self.local_host = local_host
        self.control_port = int(control_port)
        self.iq_port = int(iq_port)
        # Bind ports default to the well-known device ports (production:
        # this host talks FROM 50001/50002 like ExpertSDR3); tests pass 0
        # for ephemeral binds and learn the real ports from sockname.
        self.bind_control_port = (self.control_port if bind_control_port is None
                                  else int(bind_control_port))
        self.bind_iq_port = (self.iq_port if bind_iq_port is None
                             else int(bind_iq_port))
        self.heartbeat_s = float(heartbeat_s)
        self.boot_spacing_s = float(boot_spacing_s)
        self.recovery_idle_s = float(recovery_idle_s)
        self.recovery_cooldown_s = float(recovery_cooldown_s)
        self.tx_queue_max = int(tx_queue_max)
        self.tx_prime_packets = max(1, int(tx_prime_packets))
        self.tx_reprime_packets = max(1, int(tx_reprime_packets))
        self._tx_interval_override = (None if tx_interval_s is None
                                      else float(tx_interval_s))
        # Bounded, epoch-guarded retry for a failed PTT release: a lost
        # PTT0 is a safety defect, but the release itself must never block.
        self.ptt_release_retries = max(0, int(ptt_release_retries))
        self.ptt_release_retry_s = float(ptt_release_retry_s)
        # PTT-ON confirmation watchdog: the device confirms TX through the
        # 0x1F01 stream frame.  A lost PTT1 datagram otherwise leaves the UI
        # showing TX with a cold PA and no retry (reference field fix,
        # 2026-07-04).  Bounded and epoch-guarded: never blocks a release.
        self.ptt_confirm_timeout_s = max(0.0, float(ptt_confirm_timeout_s))
        self.ptt_confirm_retries = max(0, int(ptt_confirm_retries))
        self.on_event = on_event

        self._rate_key = _rate_key(sample_rate)
        self._freq = DEFAULT_FREQ_HZ
        self._drive_pct = DEFAULT_DRIVE_PCT
        # Boot-phase drive byte: capture-verified 0xDC until the operator
        # sets a drive, then the current byte so re-boots preserve it.
        self._boot_drive_byte = 0xDC

        self.demodulator = AudioDemodulator(sample_rate=self.sample_rate)
        self.tx_modulator = self._new_tx_modulator()
        # None = TX allowed for the current RX mode; otherwise the RX mode
        # whose transmit path is not verified (AM/FM/CW).
        self._tx_blocked_mode: Optional[str] = None
        self._apply_tx_mode()

        # RX output: bounded deque of int16-le 48 kHz blocks, always fed.
        self.rx_audio: "collections.deque[bytes]" = collections.deque(
            maxlen=int(rx_audio_max))
        # The demodulator's streaming resampler has a one-time group delay
        # of a few hundred samples, so a bare 200-sample device packet
        # yields no audio; assemble this many IQ samples per demod call
        # (~40 ms at 78k — Task 2's chunked invariance covers the seams).
        self.rx_demod_block = max(200, int(rx_demod_block))
        self._rx_buf = np.zeros(0, dtype=np.complex64)
        self._spec_iq = np.zeros(0, dtype=np.complex64)
        self.latest_telemetry: Optional[dict] = None

        # State flags.
        self.ptt_requested = False
        self.ptt_confirmed = False
        self.att_preamp = 2                       # 0dB bypass

        # Counters / observability.
        self.sent_packets = 0
        self.sent_bytes = 0
        self.recv_packets = 0
        self.recv_bytes = 0
        self.bad_packets = 0
        self.tx_drops = 0
        self.rx_drops = 0
        self.underruns = 0
        self.dsp_errors = 0
        self.sent_log: "collections.deque[tuple]" = collections.deque(
            maxlen=int(sent_log_max))
        self.sent_log_max = int(sent_log_max)

        # PTT release retry bookkeeping: a generation counter bumped on
        # every PTT operation so a stale retry can never overwrite a newer
        # key-up/key-down decision.
        self._ptt_epoch = 0
        self._ptt_release_task: Optional[asyncio.Task] = None
        self._ptt_confirm_deadline: Optional[float] = None
        self._ptt_confirm_attempts = 0

        # Resolved remote sockaddrs (host -> numeric at start(); the loop
        # never re-resolves per packet).  Each entry is (family, sockaddr).
        self._ctrl_target: Optional[tuple] = None
        self._iq_target: Optional[tuple] = None

        # Transport / task handles (None until start()).
        self._ctrl_transport: Optional[asyncio.DatagramTransport] = None
        self._iq_transport: Optional[asyncio.DatagramTransport] = None
        self._tasks: list = []
        self._started = False
        self._stream_counter = STREAM_COUNTER_START
        self._last_iq_ts = 0.0
        self._last_boot_ts = 0.0
        self._last_reboot_ts: Optional[float] = None

        # TX side.
        self._tx_queue: "collections.deque[bytes]" = collections.deque()
        self._tx_stop = asyncio.Event()
        self._tx_drain = False
        self._tx_pacer_task: Optional[asyncio.Task] = None
        self._tx_primed = False
        self._tx_prime_target = self.tx_prime_packets
        self.tx_priming_packets = 0
        self.tx_reprime_count = 0

        self._start_lock = asyncio.Lock()
        self._boot_lock = asyncio.Lock()

    # ── simple state ─────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._started

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATES[self._rate_key]

    @property
    def freq(self) -> float:
        return self._freq

    @property
    def tx_interval(self) -> float:
        """Seconds per 200-sample TX packet: 200 / (RX rate / 2)."""
        if self._tx_interval_override is not None:
            return self._tx_interval_override
        return (IQ_PAYLOAD_BYTES // 6) / (self.sample_rate / 2.0)

    @property
    def tx_active(self) -> bool:
        task = self._tx_pacer_task
        return task is not None and not task.done()

    @property
    def s_meter(self) -> Optional[float]:
        return self.demodulator.get_s_meter()

    @property
    def drive_pct(self) -> float:
        """Current DRIVE setting (percent), mirrored on every set."""
        return self._drive_pct

    @property
    def agc_mode(self) -> int:
        """Current demodulator AGC mode (0=OFF, 1=LONG, 2=SLOW, 3=MED, 4=FAST)."""
        return int(self.demodulator._agc_mode)

    @property
    def latest_iq(self) -> np.ndarray:
        """Newest IQ samples retained for the spectrum adapter."""
        return self._spec_iq

    def get_spectrum(self, bins: int = 850) -> list:
        # +IF shift: the device's IQ is LO-centred (VFO at -30500 Hz).  The
        # browser's CENTER mode and click-to-tune both treat the returned
        # frame as VFO-centred, so this must match the RX demodulator's own
        # +IF mix.  Without it every waterfall label is 30.5 kHz off.
        return iq_to_spectrum(self._spec_iq, self.sample_rate, bins=bins,
                              if_shift_hz=IF_OFFSET_HZ)

    def _new_tx_modulator(self) -> TXModulator:
        # Device fact (PROTOCOL.md §17.3): TX IQ runs at HALF the RX rate.
        return TXModulator(input_rate=AUDIO_RATE,
                           iq_rate=self.sample_rate / 2.0)

    def _next_stream_counter(self) -> int:
        """Shared 0xFFFD/0xFFFE counter; uint32 on the wire (see Task 1)."""
        self._stream_counter = (self._stream_counter
                                + STREAM_COUNTER_STEP) & 0xFFFFFFFF
        return self._stream_counter

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> bool:
        """Bind control + IQ sockets, boot the device, start housekeeping.

        Idempotent: a second call returns True without re-binding or
        re-booting.  Returns False (with a clear log and no leaked
        sockets) when the host is empty or a bind fails.
        """
        async with self._start_lock:
            if self._started:
                return True
            if not self.host:
                logger.error(
                    "SunSDR engine: no device host configured — set the "
                    "radio's address before start()")
                return False
            if self._ctrl_target is None or self._iq_target is None:
                try:
                    self._ctrl_target = await self._resolve(
                        self.control_port)
                    self._iq_target = await self._resolve(self.iq_port)
                except OSError as e:
                    logger.error(
                        "SunSDR engine: cannot resolve device host %r — %s",
                        self.host, e)
                    self._close_transports()
                    return False
            if self._ctrl_transport is None:
                try:
                    self._ctrl_transport = await self._bind_udp(
                        "control", self.bind_control_port,
                        self._ctrl_target[0])
                except OSError as e:
                    logger.error(
                        "SunSDR engine: cannot bind control UDP %s:%s — %s",
                        self.local_host or "*", self.bind_control_port, e)
                    self._close_transports()
                    return False
            if self._iq_transport is None:
                try:
                    self._iq_transport = await self._bind_udp(
                        "iq", self.bind_iq_port, self._iq_target[0])
                except OSError as e:
                    logger.error(
                        "SunSDR engine: cannot bind IQ UDP %s:%s — %s; "
                        "aborting start and releasing the control socket",
                        self.local_host or "*", self.bind_iq_port, e)
                    self._close_transports()
                    return False
            try:
                # Serialize the boot against control writes (QSY/drive/PTT
                # take the same lock) so a paced boot can never interleave
                # with a multi-packet control sequence.
                async with self._boot_lock:
                    await self._send_boot()
            except asyncio.CancelledError:
                self._close_transports()
                raise
            except Exception:
                logger.exception("SunSDR engine: boot sequence failed")
                self._close_transports()
                return False
            self._last_iq_ts = time.monotonic()
            self._started = True
            self._tasks = [
                asyncio.create_task(self._heartbeat_loop(),
                                    name="sunsdr-heartbeat"),
                asyncio.create_task(self._keepalive_loop(),
                                    name="sunsdr-keepalive"),
            ]
            self._emit("connected", {})
            logger.info("SunSDR engine: started (device %s, rate %s)",
                        self.host, self._rate_key)
            return True

    async def stop(self) -> None:
        """Idempotent stop.  Releases PTT before closing the transports.

        Everything safety-relevant happens before the first ``await``:
        transport sendto/cancel/close are synchronous, so even a cancelled
        ``stop()`` cannot skip the PTT release or leave a socket open.
        """
        if (not self._started
                and self._ctrl_transport is None
                and self._iq_transport is None):
            return
        self._started = False
        self._ptt_confirm_deadline = None
        self._ptt_confirm_attempts = 0
        tasks = list(self._tasks)
        self._tasks.clear()
        tx_task = self._tx_pacer_task
        self._tx_pacer_task = None
        if tx_task is not None:
            tasks.append(tx_task)
        retry_task = self._ptt_release_task
        self._ptt_release_task = None
        if retry_task is not None:
            tasks.append(retry_task)
        # A pending release retry means an earlier PTT0 failed; the state
        # flags are already cleared, so without this the release would be
        # skipped and the retry cancelled with the socket — the device's
        # last chance to be un-keyed lives in this final synchronous send.
        release_retry_pending = (retry_task is not None
                                 and not retry_task.done())
        try:
            if (self.ptt_requested or self.ptt_confirmed
                    or release_retry_pending):
                try:
                    self._send_control(build_ptt(False))
                except Exception as e:
                    logger.warning("SunSDR engine: PTT release failed: %s", e)
            self.ptt_requested = False
            self.ptt_confirmed = False
            self._tx_stop.set()
            self._tx_queue.clear()
            for t in tasks:
                t.cancel()
        finally:
            self._close_transports()
            self._emit("disconnected", {})
        # Let the cancelled tasks settle; if WE are cancelled here the
        # transports are already closed and the tasks will die with the loop.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _resolve(self, port: int) -> tuple:
        """Resolve the device host ONCE (getaddrinfo) and keep the numeric
        sockaddr — per-packet hostname sendto would re-run DNS inside the
        event loop for every datagram (and could stall a PTT0)."""
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(self.host, port,
                                       type=socket.SOCK_DGRAM)
        if not infos:
            raise OSError(f"no UDP address for {self.host!r}:{port}")
        family, _, _, _, sockaddr = infos[0]
        return family, sockaddr

    async def _bind_udp(self, which: str, port: int, family: int):
        loop = asyncio.get_running_loop()
        # Deliberately NO SO_REUSEADDR: on Linux it would let a second
        # instance double-bind 50001/50002 and silently steal the stream.
        sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.bind((self.local_host, port))
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _EngineDatagramProtocol(self, which), sock=sock)
        except BaseException:
            sock.close()
            raise
        bound = transport.get_extra_info("sockname")
        logger.info("SunSDR engine: %s UDP bound on %s:%s",
                    which, bound[0], bound[1])
        return transport

    def _close_transports(self):
        for t in (self._ctrl_transport, self._iq_transport):
            if t is not None:
                try:
                    t.close()
                except Exception:
                    pass
        self._ctrl_transport = None
        self._iq_transport = None

    async def _send_boot(self):
        """The captured boot sequence, paced.  Caller holds _boot_lock for
        re-boots; start() serializes via its own idempotency."""
        seq = build_boot_sequence(self._freq, rate=self._rate_key,
                                  drive=self._boot_drive_byte,
                                  att_preamp=self.att_preamp)
        for pkt in seq:
            self._send_control(pkt)
            if self.boot_spacing_s > 0:
                await asyncio.sleep(self.boot_spacing_s)
        self._last_boot_ts = time.monotonic()
        logger.info("SunSDR engine: boot complete (rate=%s, %d packets)",
                    self._rate_key, len(seq))

    # ── housekeeping loops ───────────────────────────────────────

    async def _heartbeat_loop(self):
        while True:
            # 22-byte heartbeat: u32 zero payload + zero trailing, as the
            # capture shows (PROTOCOL.md §16.2).
            self._send_control(
                build_packet(CmdID.HEARTBEAT, struct.pack("<I", 0)))
            await asyncio.sleep(self.heartbeat_s)

    async def _keepalive_loop(self):
        while True:
            self._send_stream_keepalive()
            await asyncio.sleep(self.heartbeat_s)
            await self._maybe_retry_ptt_on()
            self._maybe_recover_stream()

    def _send_stream_keepalive(self):
        hdr = struct.pack("<HHIH", MAGIC, IQ_SUBTYPE,
                          self._next_stream_counter(), KEEPALIVE_FLAGS)
        self._send_stream(hdr + b"\x00" * IQ_PAYLOAD_BYTES)

    def _maybe_recover_stream(self):
        """IQ stall watchdog → single-flight, rate-limited re-boot."""
        if not self._started or self.ptt_requested or self.tx_active:
            return
        if time.monotonic() - self._last_iq_ts < self.recovery_idle_s:
            return
        asyncio.ensure_future(self.reboot())

    async def reboot(self, force: bool = False) -> bool:
        """Controlled re-boot of the stream.  Refused while one is in
        progress (single-flight) and rate-limited unless ``force``."""
        if not self._started or self.ptt_requested or self.tx_active:
            return False
        if self._boot_lock.locked():
            return False
        now = time.monotonic()
        if (not force and self._last_reboot_ts is not None
                and now - self._last_reboot_ts < self.recovery_cooldown_s):
            return False
        self._last_reboot_ts = now
        async with self._boot_lock:
            await self._send_boot()
        return True

    # ── sending primitives ───────────────────────────────────────

    def _send_control(self, data: bytes) -> bool:
        transport = self._ctrl_transport
        target = self._ctrl_target
        if transport is None or target is None:
            return False
        try:
            transport.sendto(data, target[1])
        except Exception as e:
            logger.debug("SunSDR engine: control send failed: %s", e)
            return False
        self.sent_packets += 1
        self.sent_bytes += len(data)
        self.sent_log.append(("control", data))
        return True

    def _send_stream(self, data: bytes) -> bool:
        transport = self._iq_transport
        target = self._iq_target
        if transport is None or target is None:
            return False
        try:
            transport.sendto(data, target[1])
        except Exception as e:
            logger.debug("SunSDR engine: stream send failed: %s", e)
            return False
        self.sent_packets += 1
        self.sent_bytes += len(data)
        self.sent_log.append(("stream", data))
        return True

    def _emit(self, name: str, payload: dict):
        cb = self.on_event
        if cb is None:
            return
        try:
            cb(name, payload)
        except Exception as e:
            logger.warning("SunSDR engine: on_event(%r) failed: %s", name, e)

    # ── RX path ──────────────────────────────────────────────────

    def _on_datagram(self, which: str, data: bytes, addr):
        self.recv_packets += 1
        self.recv_bytes += len(data)
        if which != "iq":
            # Control-port replies (INFO/CALIB/STATUS responses) are not
            # part of this engine's contract; count and ignore.
            return
        try:
            parsed = parse_rx_packet(data)
        except Exception:
            parsed = None
        if parsed is None:
            self.bad_packets += 1
            return
        try:
            if parsed["kind"] == "iq":
                self._handle_iq(parsed["samples"])
            else:
                self._handle_telemetry(parsed)
        except Exception:
            # The receive path must never die on a processing error.
            logger.exception("SunSDR engine: RX processing error")

    def _handle_iq(self, samples: np.ndarray):
        self._last_iq_ts = time.monotonic()
        if samples.size:
            combined = np.concatenate([self._spec_iq, samples])
            self._spec_iq = (combined[-SPECTRUM_FFT_SIZE:]
                             if combined.size > SPECTRUM_FFT_SIZE
                             else combined)
        self._rx_buf = np.concatenate([self._rx_buf, samples])
        if self._rx_buf.size < self.rx_demod_block:
            return
        block, self._rx_buf = self._rx_buf, np.zeros(0, dtype=np.complex64)
        try:
            pcm = self.demodulator.demodulate(block)
        except Exception as e:
            self.dsp_errors += 1
            if self.dsp_errors == 1 or self.dsp_errors % 100 == 0:
                logger.warning(
                    "SunSDR engine: demodulate failed (%d times): %s",
                    self.dsp_errors, e)
            return
        if pcm:
            if len(self.rx_audio) == self.rx_audio.maxlen:
                self.rx_drops += 1      # deque evicts the oldest block
            self.rx_audio.append(pcm)

    def _handle_telemetry(self, parsed: dict):
        if "ptt" in parsed:
            confirmed = bool(parsed["ptt"])
            if confirmed and not self.ptt_requested:
                # Stale late-arriving TX frame after a release — never let
                # it re-light the confirmed flag.
                return
            if confirmed != self.ptt_confirmed:
                self.ptt_confirmed = confirmed
                if confirmed:
                    # Device is keyed: the PTT-ON watchdog has done its job.
                    self._ptt_confirm_deadline = None
                self._emit("ptt_confirmed", {"ptt": confirmed})
        else:
            self.latest_telemetry = parsed
            self._emit("telemetry", parsed)

    # ── control surface ──────────────────────────────────────────

    async def set_frequency(self, freq_hz: Union[int, float]) -> bool:
        """QSY: RX DDS, TX VFO, then re-send the current drive (the device
        resets drive to a per-band calibration value on frequency change,
        so ExpertSDR3 re-sends 0x0017 on every QSY — mirror that).  The
        three packets are serialized against boot/recovery via _boot_lock
        so a paced boot can never interleave into the sequence."""
        freq = float(freq_hz)
        async with self._boot_lock:
            ok = self._send_control(build_rx_frequency(freq))
            ok = self._send_control(build_tx_frequency(freq)) and ok
            ok = self._send_control(build_drive(self._drive_pct)) and ok
        self._freq = freq
        return ok

    async def set_drive(self, percent: Union[int, float]) -> bool:
        self._drive_pct = max(0.0, min(100.0, float(percent)))
        self._boot_drive_byte = drive_byte(self._drive_pct)
        async with self._boot_lock:
            return self._send_control(build_drive(self._drive_pct))

    async def set_ptt(self, tx: bool) -> bool:
        self._ptt_epoch += 1                     # supersede any stale retry
        self._cancel_release_retry()
        if tx:
            if self._tx_blocked_mode:
                logger.warning(
                    "SunSDR engine: TX in %s is not verified — refusing "
                    "to key (USB/LSB/DIGI only)", self._tx_blocked_mode)
                return False
            # Drive guard first: the device may have reset 0x0017 on the
            # last QSY (mirrors ExpertSDR3; without it the far end hears
            # very low power).  DRIVE+PTT1 are one atomic sequence w.r.t.
            # boot/recovery via _boot_lock.
            async with self._boot_lock:
                self._send_control(build_drive(self._drive_pct))
                ok = self._send_control(build_ptt(True))
            if not ok:
                logger.warning(
                    "SunSDR engine: PTT ON was not sent — device may not "
                    "be keyed")
                return False
            self.ptt_requested = True
            self.ptt_confirmed = False
            self._ptt_confirm_deadline = (
                time.monotonic() + self.ptt_confirm_timeout_s)
            self._ptt_confirm_attempts = 0
            # The real client's stream counter restarts at 0x04B0 per PTT
            # press (PROTOCOL.md §17.2).
            self._stream_counter = STREAM_COUNTER_START
            return True
        # Release: clear request state immediately, send PTT0 with no
        # blocking read-back and deliberately WITHOUT _boot_lock — a
        # release must never wait behind a boot.  A failed send is retried
        # in the background (bounded, epoch-guarded) because a lost PTT0
        # leaves the device keyed.
        self.ptt_requested = False
        self.ptt_confirmed = False
        self._ptt_confirm_deadline = None
        self._ptt_confirm_attempts = 0
        if not self._send_control(build_ptt(False)):
            self._schedule_release_retry(self._ptt_epoch)
            return False
        return True

    def _cancel_release_retry(self):
        task = self._ptt_release_task
        self._ptt_release_task = None
        if task is not None and not task.done():
            task.cancel()

    def _schedule_release_retry(self, epoch: int):
        if self.ptt_release_retries <= 0:
            logger.warning(
                "SunSDR engine: PTT release failed and retries are "
                "disabled — device may still be keyed")
            return
        self._ptt_release_task = asyncio.create_task(
            self._release_retry_loop(epoch), name="sunsdr-ptt-release")

    async def _release_retry_loop(self, epoch: int):
        """Bounded background retry for a failed PTT0.  Runs only while we
        are connected, the transport exists and the operator still wants
        RX; a newer PTT operation (epoch change / re-key) supersedes it."""
        tries = 0
        while tries < self.ptt_release_retries:
            try:
                await asyncio.sleep(self.ptt_release_retry_s)
            except asyncio.CancelledError:
                raise
            if epoch != self._ptt_epoch or self.ptt_requested:
                return                          # superseded by a newer PTT op
            if not self._started or self._ctrl_transport is None:
                return                          # engine stopped/closed
            if self._send_control(build_ptt(False)):
                logger.info(
                    "SunSDR engine: PTT release delivered on retry %d",
                    tries + 1)
                return
            tries += 1
        logger.warning(
            "SunSDR engine: PTT release failed after %d retries — device "
            "may still be keyed", tries)

    async def _maybe_retry_ptt_on(self):
        """Resend PTT1 once when the device has not confirmed TX in time.

        Reference field fix (2026-07-04): a silently lost UDP PTT1 left the
        UI showing TX with a cold PA and no retry.  This never blocks or
        delays a release — it only runs while ``ptt_requested`` is true and
        stops as soon as the 0x1F01 confirmation arrives.
        """
        if (not self._started or not self.ptt_requested
                or self.ptt_confirmed):
            return
        if self._ptt_confirm_attempts >= self.ptt_confirm_retries:
            return
        deadline = self._ptt_confirm_deadline
        if deadline is None or time.monotonic() < deadline:
            return
        self._ptt_confirm_attempts += 1
        self._ptt_confirm_deadline = (
            time.monotonic() + self.ptt_confirm_timeout_s)
        epoch = self._ptt_epoch
        logger.warning(
            "SunSDR engine: device did not confirm TX within %.1fs — "
            "re-sending PTT ON (attempt %d/%d)",
            self.ptt_confirm_timeout_s, self._ptt_confirm_attempts,
            self.ptt_confirm_retries)
        if not await self._resend_ptt_on(epoch):
            logger.warning("SunSDR engine: PTT ON retry was not sent")

    async def _resend_ptt_on(self, epoch: int) -> bool:
        """Drive + PTT1 retry, serialized against boot and superseded by
        any newer PTT operation (release / re-key)."""
        async with self._boot_lock:
            if epoch != self._ptt_epoch or not self.ptt_requested:
                return False
            self._send_control(build_drive(self._drive_pct))
            return self._send_control(build_ptt(True))

    async def set_mode(self, mode: str):
        """Mode is DSP-owned (AD-003 style): the demodulator always follows
        it; the TX modulator follows only the modes with a verified TX path
        (USB/LSB/DIGI).  AM/FM/CW set _tx_blocked_mode instead of silently
        leaving the previous sideband keyed (the reference TXModulator only
        implements USB/LSB for this backend)."""
        self.demodulator.set_mode(mode)
        self._apply_tx_mode()

    def _apply_tx_mode(self) -> bool:
        """Align TX modulation with the current RX mode.

        Called on mode changes AND after set_sample_rate() rebuilds the
        modulator (the fresh object defaults to USB, which used to flip a
        LSB operator's next over to USB after any span change).
        """
        rx_mode = self.demodulator.mode
        tx_mode = _TX_MODE_FOR_RX.get(rx_mode)
        if tx_mode is None:
            self._tx_blocked_mode = rx_mode
            return False
        try:
            self.tx_modulator.set_mode(tx_mode)
        except ValueError:
            self._tx_blocked_mode = rx_mode
            return False
        self._tx_blocked_mode = None
        return True

    async def set_filter(self, low_hz: float, high_hz: float):
        self.demodulator.reconfigure_filter(low_hz, high_hz)

    async def set_agc_mode(self, mode):
        self.demodulator.set_agc_mode(mode)

    # ── RX noise processing (WDSP + software NB) ────────────────

    def wdsp_available(self) -> bool:
        return self.demodulator.wdsp_available()

    async def set_noise_reduction(self, on: bool) -> bool:
        return self.demodulator.set_noise_reduction(on)

    async def set_noise_blanker(self, on: bool) -> bool:
        return self.demodulator.set_noise_blanker(on)

    async def set_auto_notch(self, on: bool) -> bool:
        return self.demodulator.set_auto_notch(on)

    async def set_nr_level(self, level: int) -> bool:
        return self.demodulator.set_nr_level(level)

    async def get_nr_level(self) -> Optional[int]:
        return self.demodulator.get_nr_level()

    async def set_nb_level(self, level: int) -> bool:
        return self.demodulator.set_nb_level(level)

    async def get_nb_level(self) -> Optional[int]:
        return self.demodulator.get_nb_level()

    async def set_att_preamp(self, level: int) -> bool:
        """Hardware ATT/preamp: SET_PARAM_5 trailing 0=-20dB, 1=-10dB,
        2=0dB bypass, 3=+10dB preamp (ExpertSDR3-verified mapping)."""
        lvl = max(0, min(3, int(level)))
        self.att_preamp = lvl
        return self._send_control(
            build_packet(CmdID.SET_PARAM_5, struct.pack("<I", 0),
                         trailing=lvl))

    async def set_sample_rate(self, rate: Union[str, int]) -> bool:
        """Switch IQ rate (needs a full re-boot: HW_INIT precedes freq and
        stream-start).  Only the four protocol rates are accepted; refused
        while TX is active; serialized so two switches never overlap."""
        key = _rate_key(rate)                     # ValueError on anything else
        if self.ptt_requested or self.tx_active:
            logger.warning(
                "SunSDR engine: sample-rate switch to %s refused: TX active",
                key)
            return False
        async with self._boot_lock:
            if self.ptt_requested or self.tx_active:
                return False
            self._rate_key = key
            hz = self.sample_rate
            self.demodulator.set_sample_rate(hz)
            # Old-rate IQ must not leak into the first frames of the new
            # span: the reference server resets its FFT accumulation buffer
            # on a rate switch for exactly this reason (2026-07-03 field
            # fix: ~4 frames of the previous span smeared into the new one).
            self._spec_iq = np.zeros(0, dtype=np.complex64)
            self._rx_buf = np.zeros(0, dtype=np.complex64)
            # TX runs at RX/2 — rebuild the whole chain for the new rate,
            # then re-apply the RX mode's TX sideband.  The fresh modulator
            # defaults to USB; without this a span change silently flipped
            # LSB operators to USB on their next over.
            self.tx_modulator = self._new_tx_modulator()
            self._apply_tx_mode()
            await self._send_boot()
        logger.info("SunSDR engine: sample rate now %s (%d Hz)", key, hz)
        return True

    # ── TX path ──────────────────────────────────────────────────

    async def start_tx(self):
        """Begin the TX pacer.  Clears stale queue + modulator state so a
        new transmission never starts with pre-PTT audio."""
        if self._tx_blocked_mode:
            logger.warning(
                "SunSDR engine: TX pacer not started — %s has no verified "
                "transmit path", self._tx_blocked_mode)
            return False
        # Idempotent: a second call while the pacer runs must NOT clear the
        # queue/reset the ramp mid-over (the server documents this contract).
        if self.tx_active:
            return True
        self.tx_modulator.reset()
        self._tx_queue.clear()
        self._tx_drain = False
        self._tx_primed = False
        self._tx_prime_target = self.tx_prime_packets
        self._tx_stop.clear()
        self._tx_pacer_task = asyncio.create_task(self._tx_pacer_loop(),
                                                  name="sunsdr-tx-pacer")
        return True

    def feed_tx_audio(self, pcm) -> int:
        """48 kHz int16 mic audio → modulated payloads into the bounded
        queue.  Returns how many payloads were queued; when the queue is
        full the OLDEST payload is dropped (counted in ``tx_drops``) —
        stale audio may be late but must never be resurrected."""
        # A mode selected mid-over may block the NEXT key-up, but the pacer
        # already running keeps the sideband it was keyed with; do not cut
        # its audio off.  (Before key-up there is no pacer, so this path is
        # only reached by a stale/other producer.)
        if self._tx_blocked_mode and not self.ptt_requested:
            return 0
        try:
            payloads = self.tx_modulator.feed_audio(pcm)
        except Exception as e:
            logger.warning("SunSDR engine: TX modulate failed: %s", e)
            return 0
        queued = 0
        for payload in payloads:
            if len(self._tx_queue) >= self.tx_queue_max:
                self._tx_queue.popleft()
                self.tx_drops += 1
            self._tx_queue.append(payload)
            queued += 1
        return queued

    async def stop_tx(self, graceful: bool = False, drain_s: float = 0.5):
        """Stop the pacer.  Default is immediate; ``graceful=True`` lets
        the already-queued audio play out, bounded by ``drain_s``."""
        task = self._tx_pacer_task
        self._tx_pacer_task = None
        if task is None or task.done():
            self._tx_queue.clear()
            return
        if graceful:
            self._tx_drain = True
            self._tx_primed = True       # drain a short final queue immediately
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=drain_s)
            except asyncio.TimeoutError:
                logger.warning(
                    "SunSDR engine: graceful TX drain hit %.2fs bound",
                    drain_s)
                self._tx_stop.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                # Our caller cancelled stop_tx mid-drain.  shield() kept
                # the cancellation away from the pacer, so without this
                # cleanup it would keep sending TX IQ while the engine
                # already reports tx_active=False — an orphan.  Flag-stop
                # and reap it before letting the cancellation propagate.
                self._tx_stop.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        else:
            self._tx_stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._tx_queue.clear()

    def _send_tx_packet(self, payload: Optional[bytes]):
        if payload is None:
            payload = b"\x00" * IQ_PAYLOAD_BYTES       # underrun silence
        hdr = struct.pack("<HHIH", MAGIC, TX_SUBTYPE,
                          self._next_stream_counter(), TX_FLAGS)
        self._send_stream(hdr + payload)

    async def _tx_pacer_loop(self):
        """Fixed-rate pacer: one 200-sample packet per ``tx_interval``
        (200/(RX rate/2) — the device DAC rate, PROTOCOL.md §17.3)."""
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while True:
            if self._tx_stop.is_set():
                break
            if not self._tx_primed and len(self._tx_queue) >= self._tx_prime_target:
                self._tx_primed = True
            if self._tx_primed and self._tx_queue:
                self._send_tx_packet(self._tx_queue.popleft())
            elif self._tx_drain:
                break                       # graceful: queue exhausted
            else:
                if self._tx_primed:
                    # A real gap after playback started: count it once, then
                    # re-prime instead of sending isolated old/new splices.
                    self.underruns += 1
                    self._tx_primed = False
                    self._tx_prime_target = self.tx_reprime_packets
                    self.tx_reprime_count += 1
                else:
                    self.tx_priming_packets += 1
                self._send_tx_packet(None)
            next_t += self.tx_interval
            delay = next_t - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                if delay < -self.tx_interval:
                    # fell badly behind — re-anchor, don't burst-catch-up
                    next_t = loop.time() + self.tx_interval
