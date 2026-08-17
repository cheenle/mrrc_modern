"""
Icom CI-V protocol codec (pure functions — no serial I/O, no asyncio)
=====================================================================
Binary framing, incremental stream parsing, BCD helpers and the
0x27 0x00 scope-waveform disassembler/reassembler for Icom radios
(IC-7300 address 0x94 by default).

Frame layout on the wire::

    FE FE <to> <from> <cmd> [sub/data ...] FD

On a simplex CI-V bus every transmitted frame is echoed back, so a
reader sees our own frames too — use :func:`is_echo` to drop them.

Sources (byte-level facts verified against these):

- Icom "IC-7300 INFORMATION / CI-V Reference" (IC-7300_ENG_Info_V140_0.pdf),
  pp. 9-10: scope waveform data (27 00), span setting (27 15),
  scope mode (27 14), fixed edge (27 16 / 27 1E).
- wfview ``src/radio/icomudpcivdata.cpp`` (github mirror eliggett/wfview):
  confirms the scope sequence/division-max bytes are BCD-encoded on the
  wire (division 11 -> byte 0x11; see the BCD re-encode at lines ~202-208)
  and documents the real IC-7300 chunk layout in its comments.
- wfview ``rigs/IC-7300.rig``: CIVAddress=148 (0x94),
  SpectrumSeqMax=11, SpectrumAmpMax=160, SpectrumLenMax=475.

Level values (AF gain, RF power, meter readouts, ...) are exchanged as
two BCD bytes, MOST significant byte first (hundreds+tens in byte 0,
units in byte 1): 255 -> 0x02 0x55.  This is the long-standing Icom
CI-V convention (documented in every Icom CI-V command table, e.g. the
0x14 level commands, range "0000-0255") and matches hamlib's
big-endian ``from_bcd``/``to_bcd`` use for Icom levels.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

# ── Framing ─────────────────────────────────────────────────────────
PREAMBLE = b"\xfe\xfe"
END_OF_MESSAGE = 0xFD
CONTROLLER_ADDR = 0xE0          # this server, per CI-V convention
RADIO_ADDR = 0x94               # IC-7300 factory default CI-V address
OK = 0xFB                       # command accepted (reply body)
NG = 0xFA                       # command rejected (reply body)

# Parser guard: CI-V command frames over the IC-7300's USB serial port
# are small (longest regular traffic = scope chunks, <= ~57 bytes).
# LAN-style single-blob scope frames (~490 B) never appear on serial.
DEFAULT_MAX_FRAME = 64

# ── Scope (cmd 0x27) protocol facts ─────────────────────────────────
SCOPE_CMD = 0x27
SCOPE_SUB_DATA = 0x00           # waveform data output
SCOPE_WAVEFORM_LEN = 475        # bins per complete waveform
SCOPE_AMPLITUDE_MAX = 160       # bin value range 0..160
SCOPE_MAX_SEGMENTS = 11         # USB-serial delivery (LAN sends 1 blob)

# Scope mode codes (first info byte of a sequence-1 segment)
SCOPE_MODE_CENTER = 0x00
SCOPE_MODE_FIXED = 0x01
SCOPE_MODE_SCROLL_C = 0x02
SCOPE_MODE_SCROLL_F = 0x03


@dataclass
class CivFrame:
    """One decoded CI-V frame (addresses, command, payload)."""

    to: int
    from_addr: int
    command: int
    data: bytes = b""           # everything between command byte and FD

    def to_bytes(self) -> bytes:
        return (
            PREAMBLE
            + bytes((self.to, self.from_addr, self.command))
            + self.data
            + bytes((END_OF_MESSAGE,))
        )

    def __bytes__(self) -> bytes:
        return self.to_bytes()


def build_frame(
    command: int,
    data: bytes = b"",
    to: int = RADIO_ADDR,
    from_addr: int = CONTROLLER_ADDR,
) -> bytes:
    """Serialize a CI-V frame (preamble + addresses + cmd + data + FD)."""
    return CivFrame(to=to, from_addr=from_addr, command=command, data=data).to_bytes()


class CivFrameParser:
    """Incremental CI-V byte-stream parser.

    Feed arbitrary chunks of the serial stream; complete frames are
    returned as :class:`CivFrame` objects.  Robust to:

    - garbage / partial bytes before a preamble (counted in
      ``discarded_bytes``),
    - frames split across :meth:`feed` calls,
    - back-to-back frames in one chunk,
    - a new ``FE FE`` preamble arriving mid-frame (resync: the
      incomplete frame is discarded),
    - oversize frames (> ``max_frame_size`` bytes without FD: discarded).

    The first 0xFD ends the frame.  0xFD never legitimately appears in
    IC-7300 scope data (waveform values are 0..160 = 0xA0 max), so no
    escaping is needed.
    """

    def __init__(self, max_frame_size: int = DEFAULT_MAX_FRAME):
        self.max_frame_size = max_frame_size
        self.discarded_bytes = 0
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[CivFrame]:
        self._buf.extend(data)
        frames: list[CivFrame] = []
        buf = self._buf
        while True:
            # Locate the next preamble.
            start = buf.find(PREAMBLE)
            if start < 0:
                # No preamble: keep a trailing 0xFE (could be the first
                # half of a split preamble), discard the rest.
                keep = 1 if buf.endswith(b"\xfe") else 0
                self.discarded_bytes += len(buf) - keep
                del buf[: len(buf) - keep]
                break
            if start > 0:
                self.discarded_bytes += start
                del buf[:start]
            # Need at least preamble + to + from + cmd + FD.
            if len(buf) < len(PREAMBLE) + 4:
                break  # wait for more bytes
            # Scan for the end-of-frame FD, watching for a resync
            # preamble and the size guard.  (Index buf directly — a
            # live memoryview would block resizing below.)
            off = len(PREAMBLE)
            body_len = len(buf) - off
            end = -1
            resync = -1      # new FE FE preamble found mid-frame
            corrupt = False  # 0xFE inside the to/from/cmd header area
            scan_limit = min(body_len, self.max_frame_size)
            i = 0
            while i < scan_limit:
                b = buf[off + i]
                if b == END_OF_MESSAGE:
                    end = i
                    break
                if b == 0xFE:
                    if i > 0 and off + i + 1 < len(buf) and buf[off + i + 1] == 0xFE:
                        resync = i  # new preamble mid-frame
                        break
                    if i < 3:
                        # 0xFE is never a valid address/command byte, so
                        # this "preamble" was a false start (e.g. an
                        # overlapping FE FE FE sequence).  Drop one byte
                        # and re-anchor on the following FE.
                        corrupt = True
                        break
                i += 1
            if corrupt:
                self.discarded_bytes += 1
                del buf[:1]
                continue
            if resync >= 0:
                # Discard the false start; loop re-anchors on the new
                # preamble (its FE FE stays in the buffer).
                self.discarded_bytes += len(PREAMBLE) + resync
                del buf[: len(PREAMBLE) + resync]
                continue
            if end < 0:
                if body_len > self.max_frame_size:
                    # Oversize: drop preamble + scanned bytes, rescan.
                    self.discarded_bytes += len(PREAMBLE) + scan_limit
                    del buf[: len(PREAMBLE) + scan_limit]
                    continue
                break  # incomplete frame; wait for more data
            if end < 3:
                # Fewer than to/from/cmd bytes — malformed, discard.
                self.discarded_bytes += len(PREAMBLE) + end + 1
                del buf[: len(PREAMBLE) + end + 1]
                continue
            frames.append(
                CivFrame(
                    to=buf[off],
                    from_addr=buf[off + 1],
                    command=buf[off + 2],
                    data=bytes(buf[off + 3 : off + end]),
                )
            )
            del buf[: len(PREAMBLE) + end + 1]
        return frames


# ── BCD helpers ─────────────────────────────────────────────────────

def encode_freq_bcd(hz: int) -> bytes:
    """Encode a frequency in Hz as 5 BCD bytes, least significant first.

    Byte 0 = (10 Hz digit << 4) | 1 Hz digit ... byte 4 = (10 GHz << 4)
    | 1 GHz digit.  Example: 14_074_000 Hz -> 00 40 07 14 00 (matches
    the layout in the Icom CI-V reference, p. 10, and on-air captures).
    """
    if not 0 <= hz <= 9_999_999_999:
        raise ValueError(f"frequency out of 5-byte BCD range: {hz}")
    out = bytearray(5)
    for i in range(5):
        low = (hz // 10 ** (2 * i)) % 10
        high = (hz // 10 ** (2 * i + 1)) % 10
        out[i] = (high << 4) | low
    return bytes(out)


def decode_freq_bcd(data: bytes) -> int:
    """Decode up to 5 little-endian BCD digit-pair bytes into Hz."""
    if len(data) > 5:
        raise ValueError(f"frequency BCD longer than 5 bytes: {len(data)}")
    hz = 0
    for i, b in enumerate(data):
        hz += (b & 0x0F) * 10 ** (2 * i)
        hz += (b >> 4) * 10 ** (2 * i + 1)
    return hz


def encode_level_bcd(value: int) -> bytes:
    """Encode a 0-255 level as 2 BCD bytes, high byte first on the wire.

    255 -> b"\\x02\\x55" (hundreds+tens in byte 0, units in byte 1).
    Source: Icom CI-V level-command convention (0x14 levels are
    documented as 4-digit BCD "0000"-"0255", transmitted MSB first);
    hamlib uses big-endian BCD for Icom levels the same way.
    """
    if not 0 <= value <= 255:
        raise ValueError(f"level out of range 0-255: {value}")
    return bytes((value // 100, ((value // 10) % 10) << 4 | (value % 10)))


def decode_level_bcd(data: bytes) -> int:
    """Decode 1-2 big-endian BCD bytes (as sent by Icom) into 0-255."""
    if not 1 <= len(data) <= 2:
        raise ValueError(f"level BCD must be 1-2 bytes: {len(data)}")
    if len(data) == 1:
        return ((data[0] >> 4) * 10) + (data[0] & 0x0F)
    return data[0] * 100 + ((data[1] >> 4) * 10) + (data[1] & 0x0F)


def is_echo(frame: CivFrame, last_sent: bytes) -> bool:
    """True when *frame* is the bus echo of our own transmission.

    On the simplex CI-V bus our frames come back with from_addr =
    CONTROLLER_ADDR and byte-identical content.
    """
    return frame.from_addr == CONTROLLER_ADDR and bytes(frame) == last_sent


# ── Scope waveform (cmd 0x27, sub 0x00) ─────────────────────────────

def _bcd_byte_to_int(b: int) -> int:
    """Decode one BCD byte (0x11 -> 11).  Icom sends the scope sequence
    and division-max bytes BCD-encoded; verified against wfview
    icomudpcivdata.cpp which BCD-encodes division 11 as byte 0x11, and
    against real IC-7300 captures (chunk #11 starts `27 00 00 11 11`)."""
    return (b >> 4) * 10 + (b & 0x0F)


@dataclass
class ScopeSegment:
    """One 0x27 0x00 scope chunk.

    A complete waveform (475 bins) arrives as up to 11 segments over
    USB serial.  Segment 1 (``is_division_start``) carries only the
    waveform info (scope mode + frequency range); segments 2..11 carry
    up to 50 bins each (last one 25).  A LAN radio sends a single
    segment with ``sequence == sequence_max == 1`` holding all bins.

    Sources: Icom IC-7300 CI-V reference p. 10; layout cross-checked
    against the annotated real captures in wfview icomudpcivdata.cpp.
    """

    sequence: int
    sequence_max: int
    bins: bytes
    is_division_start: bool = False
    # Waveform info, decoded from the sequence-1 segment (None otherwise):
    scope_mode: Optional[int] = None        # SCOPE_MODE_* constant
    center_freq_hz: Optional[int] = None    # center / scroll-c modes
    span_hz: Optional[int] = None
    low_edge_hz: Optional[int] = None       # fixed / scroll-f modes
    high_edge_hz: Optional[int] = None
    out_of_range: bool = False

    @property
    def is_last(self) -> bool:
        return self.sequence == self.sequence_max


def parse_scope_segment(frame: CivFrame) -> Optional[ScopeSegment]:
    """Parse a CI-V frame into a :class:`ScopeSegment`, or None when the
    frame is not a 0x27 0x00 scope-data message.

    Data layout after the command byte (Icom CI-V reference p. 10;
    byte offsets include the sub-command byte)::

        data[0]  sub command: 0x00
        data[1]  receiver: 0x00 = main (IC-7300 is single-receiver)
        data[2]  sequence number, BCD: 0x01..0x11
        data[3]  division maximum, BCD: 0x11 for USB serial, 0x01 for LAN
        seq 1:   data[4] scope mode, data[5:10] center/low freq BCD,
                 data[10:15] span/high freq BCD, data[15] out-of-range
        seq >=2: data[4:] waveform bins (amplitudes 0..160)
    """
    if frame.command != SCOPE_CMD:
        return None
    d = frame.data
    if len(d) < 4 or d[0] != SCOPE_SUB_DATA:
        return None
    seq = _bcd_byte_to_int(d[2])
    seq_max = _bcd_byte_to_int(d[3])
    if seq < 1 or seq_max < 1 or seq > seq_max:
        return None
    seg = ScopeSegment(
        sequence=seq,
        sequence_max=seq_max,
        bins=b"" if seq == 1 else bytes(d[4:]),
        is_division_start=(seq == 1),
    )
    if seq == 1:
        # Info chunk: decode mode + frequency range; bins normally empty.
        if len(d) >= 15:
            seg.scope_mode = d[4]
            if seg.scope_mode in (SCOPE_MODE_CENTER, SCOPE_MODE_SCROLL_C):
                seg.center_freq_hz = decode_freq_bcd(d[5:10])
                seg.span_hz = decode_freq_bcd(d[10:15])
            else:
                seg.low_edge_hz = decode_freq_bcd(d[5:10])
                seg.high_edge_hz = decode_freq_bcd(d[10:15])
            seg.out_of_range = len(d) > 15 and d[15] == 0x01
            # Per the Icom doc the waveform data is omitted when the
            # segment is out of range; anything after byte 15 is bins.
            seg.bins = bytes(d[16:])
    return seg


class ScopeAssembler:
    """Reassemble a complete scope waveform from scope segments.

    Feed segments in arrival order; returns the full bin list (475 bins
    in fixed mode, up to 475 in center mode) when the final segment
    arrives, else None.  A sequence gap, duplicate, or mismatched
    sequence_max drops the whole in-progress waveform (reset).
    """

    def __init__(self):
        self._reset()

    def _reset(self) -> None:
        self._seq_max: Optional[int] = None
        self._expected = 0
        self._bins = bytearray()

    def feed(self, segment: ScopeSegment) -> Optional[list[int]]:
        if segment.sequence == 1:
            # Division start: begin a fresh waveform.
            self._reset()
            if segment.sequence_max == 1:
                # LAN-style single segment carries the whole waveform.
                return list(segment.bins) if segment.bins else None
            self._seq_max = segment.sequence_max
            self._expected = 2
            self._bins.extend(segment.bins)  # normally empty
            return None
        if self._seq_max is None:
            return None  # mid-waveform segment without a start: drop
        if segment.sequence != self._expected or segment.sequence_max != self._seq_max:
            self._reset()  # gap / duplicate / discontinuity: drop waveform
            return None
        self._bins.extend(segment.bins)
        if segment.sequence == self._seq_max:
            complete = list(self._bins)
            self._reset()
            return complete
        self._expected += 1
        return None


def scale_scope_bins(
    bins: Sequence[int], in_max: int = SCOPE_AMPLITUDE_MAX, out_max: int = 255
) -> list[int]:
    """Linear amplitude rescale (default 0..160 -> 0..255), clamped."""
    if in_max <= 0:
        raise ValueError("in_max must be positive")
    scaled = []
    for b in bins:
        v = round(b * out_max / in_max)
        scaled.append(max(0, min(out_max, v)))
    return scaled


def upsample_bins(bins: Sequence[int], out_len: int = 850) -> list[int]:
    """Nearest-neighbor upsample of a bin sequence to *out_len* points."""
    n = len(bins)
    if n == 0 or out_len <= 0:
        return []
    return [bins[min(n - 1, i * n // out_len)] for i in range(out_len)]
