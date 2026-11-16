"""VITA-49 wire codec for the FlexRadio SmartSDR streams.

Pure stdlib + numpy: no sockets, no asyncio, no app imports — every function
here is testable from bytes alone (``tests/test_flex_vita.py``).

Facts and their sources live in ``docs/flexradio-smartsdr-analysis.md`` §5-§6:

* 28-byte header, seven big-endian 32-bit words.  Word 0 packs
  ``type | C | T | TSI | TSF | packet count (4 bits) | size (32-bit words)``.
* The **stream class is identified by the PCC** (low 16 bits of class word 3),
  never by the packet type.
* ``0x03E3`` RX audio — interleaved **float32 stereo, big-endian, 24 kHz**.
  ``0x0123`` is the reduced-bandwidth DAX variant (int16 mono).
* ``0x8003`` panadapter FFT — a 12-byte sub-header plus uint16 bins, possibly
  split across packets.  **The bins are pixel Y positions, not dBm** (§5.2).
* ``0x8002`` meters — ``N × (uint16 id, int16 raw)``.
* ``0x8005`` is Opus in both directions; the client→radio layout is built here
  (§6) because the radio forces Opus for ``remote_audio_tx``.

The one invariant this module exists to protect: with the pan asked for
``min_dbm=-120 max_dbm=0 ypixels=256``, a UI byte is exactly ``255 - pixel``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

__all__ = [
    "VitaError", "Header", "parse_header", "packet_pcc",
    "PCC_FFT", "PCC_WATERFALL", "PCC_RX_AUDIO", "PCC_DAX_AUDIO",
    "PCC_OPUS", "PCC_METERS", "DAX_IQ_RATES",
    "FFT_SUBHEADER_BYTES", "FFT_SUBHEADER_CANDIDATES", "FFT_SUBHEADER_WORDS",
    "FftSegment", "parse_fft_segment",
    "FftAssembler",
    "parse_audio", "audio_mono", "parse_meters", "parse_dax_audio",
    "build_opus_packet", "OPUS_OUI", "ICC",
    "PAN_MIN_DBM", "PAN_MAX_DBM", "PAN_YPIXELS", "UI_MIN_DBM", "UI_MAX_DBM",
    "pixels_to_db", "db_to_ui", "pixels_to_ui",
    "DISCOVERY_CLASS_ID", "parse_discovery_payload_bytes", "DISCOVERY_STREAM_ID",
]

HEADER_BYTES = 28

# ── packet class codes (PCC) ─────────────────────────────────────────
PCC_FFT = 0x8003
PCC_WATERFALL = 0x8004
PCC_RX_AUDIO = 0x03E3          # IF_NARROW: float32 stereo, 24 kHz
PCC_DAX_AUDIO = 0x0123         # NARROW_REDUCED_BW: int16 mono
PCC_OPUS = 0x8005
PCC_METERS = 0x8002

#: DAX IQ class codes carry the IQ rate in the low byte (0x02E3..0x02E6).
DAX_IQ_RATES: Dict[int, int] = {
    0x02E3: 24000,
    0x02E4: 48000,
    0x02E5: 96000,
    0x02E6: 192000,
}

#: FlexRadio OUI in word 2 and the interface class code in word 3.
OPUS_OUI = 0x00001C2D
ICC = 0x534C

#: Discovery broadcasts: VITA ext-data, stream 0x800, class 0x534CFFFF.
DISCOVERY_CLASS_ID = 0x534CFFFF
DISCOVERY_STREAM_ID = 0x800

#: The dBm aperture we ask every pan for (design §6).  With these three values
#: ``pixels_to_ui`` collapses to ``255 - pixel`` — one subtraction, no float.
#:
#: Upper bound moved from 0 dBm to **-40 dBm** (2026-11-16): a 120 dB window spends
#: its top 40 dB on signals that never arrive while an HF noise floor lands on the
#: **bottom two counts** of a 256-level scale — a per-bin floor of ~-119 dBm (100 kHz
#: span, real antenna) rendered as UI 1-3/255, i.e. an empty waterfall under a radio
#: that is receiving perfectly well.  -130..-40 (90 dB) puts that floor at UI ≈ 32
#: (visible grass) and still leaves 30 dB of headroom above an S9 signal.
PAN_MIN_DBM = -140.0
PAN_MAX_DBM = -50.0
PAN_YPIXELS = 256

#: The window the **browser byte** is computed against.  This is what decides how
#: bright the waterfall is — the pan aperture above only changes the radio's
#: quantisation.  It used to be [-120, 0] dB ("matching the SunSDR producer"),
#: which spends the top 40 dB on signals that never exist and renders a real HF
#: noise floor (≈ -119 dBm per bin at a 100 kHz span) as UI 1-3/255 — a *black*
#: waterfall under a radio that receives fine (field 2026-11-16: "LSB 有声音，
#: 瀑布看不到信号").  -130..-50 (80 dB) puts that floor at UI ≈ 36 (visible grass),
#: an S9 signal at ≈ 181, and still clips only what is louder than -50 dBm.
UI_MIN_DBM = -140.0
UI_MAX_DBM = -60.0

#: The radio's FFT puts a **fixed** bright structure at both ends of every frame
#: (measured 2026-11-16: bins 5-10 = 105/148/192/237/255/91 and bins 18-23 =
#: 253/255/236/208/180/151, *identical at 7.074, 9.850 and 14.270 MHz* — so it is
#: an artefact of the transform's edges, not RF).  SmartSDR hides it; we replace
#: those bins with the frame's interior median so it stops looking like two
#: ghosts parked at the left edge.
FFT_EDGE_HEAD_BINS = 26
FFT_EDGE_TAIL_BINS = 12

FFT_SUBHEADER_BYTES = 12
#: (start_bin, num_bins, bin_size, total_bins, frame_index) — five uint16
#: fields.  The published description says the sub-header is 12 bytes, which
#: leaves two bytes unaccounted for; 10 bytes is the other reading of the same
#: five fields.  Both are tried (12 first) and the one that agrees with the
#: payload length wins, so a wrong guess degrades to "whole packet = one frame"
#: instead of drawing a half-shifted curve (design §6, U3).
FFT_SUBHEADER_CANDIDATES: Tuple[int, ...] = (12, 10)
FFT_SUBHEADER_WORDS = 5


class VitaError(ValueError):
    """Raised for a packet that cannot be a well-formed Flex VITA-49 frame."""


@dataclass(frozen=True)
class Header:
    """Parsed 28-byte VITA-49 header, plus the payload it declares."""

    packet_type: int
    class_id_present: bool
    trailer_present: bool
    tsi: int
    tsf: int
    packet_count: int
    size_words: int
    stream_id: int
    icc: int
    pcc: int
    payload: bytes
    trailer: bytes = b""


def parse_header(data: bytes) -> Header:
    """Parse one VITA-49 packet.

    ``size_words`` counts 32-bit words of the whole packet (header included);
    a truncated packet is clamped to the bytes actually received rather than
    rejected, because the Opus encoder deliberately does not pad payloads to a
    word boundary and the declared size is rounded up (analysis §6).
    """
    if len(data) < HEADER_BYTES:
        raise VitaError(f"short VITA packet: {len(data)} < {HEADER_BYTES}")

    word0, stream_id, _, class_word = struct.unpack_from(">IIII", data, 0)
    packet_type = (word0 >> 28) & 0x0F
    class_id_present = bool((word0 >> 27) & 0x01)
    trailer_present = bool((word0 >> 26) & 0x01)
    tsi = (word0 >> 22) & 0x03
    tsf = (word0 >> 20) & 0x03
    packet_count = (word0 >> 16) & 0x0F
    size_words = word0 & 0xFFFF

    declared = size_words * 4 if size_words else len(data)
    end = min(declared, len(data))
    trailer_len = 4 if trailer_present else 0
    if end - HEADER_BYTES - trailer_len < 0:
        raise VitaError(
            f"declared size {declared} leaves no payload (C={class_id_present}, "
            f"T={trailer_present}, have {len(data)})")

    payload_end = end - trailer_len
    return Header(
        packet_type=packet_type,
        class_id_present=class_id_present,
        trailer_present=trailer_present,
        tsi=tsi,
        tsf=tsf,
        packet_count=packet_count,
        size_words=size_words,
        stream_id=stream_id,
        icc=(class_word >> 16) & 0xFFFF,
        pcc=class_word & 0xFFFF,
        payload=data[HEADER_BYTES:payload_end],
        trailer=data[payload_end:end] if trailer_len else b"",
    )


def packet_pcc(data: bytes) -> int:
    """The PCC of a packet, or 0 when it carries no class id."""
    return parse_header(data).pcc


# ── FFT (0x8003) ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class FftSegment:
    """One slice of a panadapter FFT frame.

    ``bins`` is a tuple of uint16 values: **pixel Y positions**, 0 = top
    (= ``max_dbm``) down to ``ypixels - 1`` (= ``min_dbm``).  They are not dBm.
    """

    start_bin: int
    num_bins: int
    bin_size: int
    total_bins: int
    frame_index: int
    bins: Tuple[int, ...]
    #: Sub-header length actually used (12 or 10); 0 when the packet was read
    #: as one whole frame.  Diagnostics only — hardware decides which is real.
    subheader_bytes: int = 0


def parse_fft_segment(payload: bytes) -> FftSegment:
    """Parse the FFT sub-header plus its uint16 bins.

    The sub-header's exact layout is only documented indirectly for v1.4.0.0
    (five uint16 fields inside 10 or 12 bytes), so both lengths are tried and
    only a reading that agrees with the payload length is accepted.  Anything
    else falls back to "this packet is one whole frame" — a wrong guess must
    never produce a plausible but half-shifted spectrum (design §6, U3).
    """
    if len(payload) < 4:
        raise VitaError(f"short FFT payload: {len(payload)}")

    if len(payload) >= 10:
        start_bin, num_bins, bin_size, total_bins, frame_index = \
            struct.unpack_from(">HHHHH", payload, 0)
        for head_len in FFT_SUBHEADER_CANDIDATES:
            body = payload[head_len:]
            if (num_bins > 0
                    and total_bins >= num_bins
                    and start_bin + num_bins <= total_bins
                    and len(body) == num_bins * 2):
                bins = np.frombuffer(body, dtype=">u2")
                return FftSegment(start_bin=start_bin, num_bins=num_bins,
                                  bin_size=bin_size, total_bins=total_bins,
                                  frame_index=frame_index,
                                  bins=tuple(bins.tolist()),
                                  subheader_bytes=head_len)

    bins_len = len(payload) // 2
    if bins_len <= 0:
        raise VitaError("FFT packet carries no bins")
    bins = np.frombuffer(payload[: bins_len * 2], dtype=">u2")
    return FftSegment(start_bin=0, num_bins=bins_len, bin_size=0,
                      total_bins=bins_len, frame_index=0,
                      bins=tuple(bins.tolist()), subheader_bytes=0)


class FftAssembler:
    """Stitch FFT segments into whole frames.

    A frame is complete when every bin of ``total_bins`` arrived.  Partial
    frames are kept until a newer ``frame_index``/``total_bins`` shows up, then
    discarded and counted — a stalled pan must not pin memory forever.
    """

    def __init__(self) -> None:
        self._total = 0
        self._frame_index = -1
        self._bins: List[int] = []
        self._filled = 0
        self.dropped_frames = 0

    def feed(self, segment: FftSegment) -> Optional[np.ndarray]:
        if (segment.total_bins != self._total
                or segment.frame_index != self._frame_index):
            if self._filled and self._filled < self._total:
                self.dropped_frames += 1
            self._total = segment.total_bins
            self._frame_index = segment.frame_index
            self._bins = [0] * self._total
            self._filled = 0
        end = segment.start_bin + segment.num_bins
        if segment.start_bin < 0 or end > self._total:
            raise VitaError(
                f"FFT segment [{segment.start_bin},{end}) outside frame of "
                f"{self._total} bins")
        self._bins[segment.start_bin:end] = segment.bins
        self._filled += segment.num_bins
        if self._filled < self._total:
            return None
        frame = np.asarray(self._bins, dtype=np.uint16)
        self._filled = 0
        return frame


# ── audio (0x03E3 / 0x0123) ──────────────────────────────────────────

def parse_audio(payload: bytes) -> np.ndarray:
    """``0x03E3`` payload → ``(n, 2)`` float32 array (native endian).

    Interleaved big-endian float32 stereo at 24 kHz; a payload whose length is
    not a multiple of 8 cannot be that and is rejected instead of guessed at.
    """
    if len(payload) % 8:
        raise VitaError(f"audio payload {len(payload)}B is not float32 stereo")
    frames = np.frombuffer(payload, dtype=">f4")
    return np.ascontiguousarray(frames.reshape(-1, 2).astype(np.float32, copy=True))


def audio_mono(frames: np.ndarray) -> np.ndarray:
    """Downmix to mono by averaging.

    The box's whole audio pipeline is mono (AD-004/AD-011); the cost is that
    ``slice audio_pan`` left/right placement is not reproduced (design §5.1).
    """
    if frames.size == 0:
        return np.zeros(0, dtype=np.float32)
    return frames.astype(np.float32, copy=False).mean(axis=1, dtype=np.float32)


def parse_dax_audio(payload: bytes) -> np.ndarray:
    """``0x0123`` payload → float32 mono samples in [-1, 1]."""
    if len(payload) % 2:
        raise VitaError(f"DAX audio payload {len(payload)}B is not int16 mono")
    return (np.frombuffer(payload, dtype=">i2").astype(np.float32) / 32768.0)


# ── meters (0x8002) ──────────────────────────────────────────────────

def parse_meters(payload: bytes) -> Dict[int, int]:
    """``0x8002`` payload → ``{meter_id: signed raw value}``.

    Meter ids are assigned by the radio per session, so callers must resolve
    them through the ``meter`` status definitions by name (analysis §3.3).
    """
    if len(payload) % 4:
        raise VitaError(f"meter payload {len(payload)}B is not 4-byte pairs")
    out: Dict[int, int] = {}
    for offset in range(0, len(payload), 4):
        meter_id, raw = struct.unpack_from(">Hh", payload, offset)
        out[meter_id] = raw
    return out


# ── client → radio Opus (0x8005) ─────────────────────────────────────

def build_opus_packet(stream_id: int, opus: bytes) -> bytes:
    """Build the VITA-49 Opus packet the radio accepts on ``remote_audio_tx``.

    Byte-exact per analysis §6: 28-byte header, no trailer, and **no padding**
    (the header's size field is still rounded up to 32-bit words, because that
    is what the field measures), timestamps all zero.
    """
    if not opus:
        raise VitaError("refusing to build an empty Opus packet")
    packet_bytes = HEADER_BYTES + len(opus)
    size_words = (packet_bytes + 3) // 4
    word0 = ((3 << 28)            # packet type 3 = ExtDataWithStream
             | (1 << 27)          # C: class id present
             | (3 << 22)          # TSI = 3
             | (1 << 20)          # TSF = 1
             | size_words)
    header = struct.pack(">IIIIIII", word0, stream_id & 0xFFFFFFFF,
                         OPUS_OUI, (ICC << 16) | PCC_OPUS, 0, 0, 0)
    return header + bytes(opus)


# ── spectrum mapping (design §6) ─────────────────────────────────────

def pixels_to_db(pixels, *, ypixels: int = PAN_YPIXELS,
                 max_dbm: float = PAN_MAX_DBM,
                 min_dbm: float = PAN_MIN_DBM) -> np.ndarray:
    """Pixel Y → dBm, per the radio's own aperture.

    ``dBm = max - pixel / (ypixels - 1) * (max - min)``
    """
    if ypixels < 2:
        raise ValueError("ypixels must be >= 2")
    arr = np.asarray(pixels, dtype=np.float64)
    return max_dbm - arr / (ypixels - 1.0) * (max_dbm - min_dbm)


def db_to_ui(db, *, ui_min: float = UI_MIN_DBM,
             ui_max: float = UI_MAX_DBM) -> np.ndarray:
    """dBm → the 0..255 byte the browser's waterfall/spectrum expects."""
    if ui_max <= ui_min:
        raise ValueError("ui_max must exceed ui_min")
    arr = np.asarray(db, dtype=np.float64)
    scaled = (arr - ui_min) / (ui_max - ui_min) * 255.0
    return np.clip(np.rint(scaled), 0, 255).astype(np.uint8)


def pixels_to_ui(pixels, *, ypixels: int = PAN_YPIXELS,
                 max_dbm: float = PAN_MAX_DBM,
                 min_dbm: float = PAN_MIN_DBM) -> np.ndarray:
    """Pixel Y → UI byte with the pan aperture we request.

    With the default ``min=-120 / max=0 / ypixels=256`` this is exactly
    ``255 - pixel``; the general path is kept so a radio that insists on
    another aperture still maps correctly.  ``tests/test_flex_vita.py`` pins
    the identity.
    """
    return db_to_ui(pixels_to_db(pixels, ypixels=ypixels, max_dbm=max_dbm,
                                 min_dbm=min_dbm))


# ── discovery envelope ───────────────────────────────────────────────

def parse_discovery_payload_bytes(data: bytes) -> Optional[bytes]:
    """Return the text payload of a discovery broadcast, or None.

    The radio broadcasts once per second from UDP 4992 as a VITA ext-data
    packet with stream 0x800 and class 0x534CFFFF (analysis §1) — the caller
    only has to listen.
    """
    try:
        header = parse_header(data)
    except VitaError:
        return None
    if ((header.icc << 16 | header.pcc) != DISCOVERY_CLASS_ID
            or header.stream_id != DISCOVERY_STREAM_ID):
        return None
    return header.payload.rstrip(b"\x00")
