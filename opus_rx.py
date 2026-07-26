"""
RX Opus encoder for FT-710 web audio
=====================================
Server-side Opus encoder for the RX audio stream. Compresses the 48 kHz mono
Int16 PCM broadcast (~768 kbit/s) down to 64 kbit/s by default — a 12x
bandwidth cut for mobile clients.

WHY A DIRECT CTYPES WRAPPER (not opuslib), AND NO ctl CALLS:
  `opus_encoder_ctl` is a C variadic function. On arm64 (Apple Silicon) the
  variadic ABI passes the trailing arg on the stack, but ctypes with pinned
  argtypes passes it in a register — so every SET ctl returns OPUS_BAD_ARG (-1)
  and bitrate control silently no-ops (opuslib hits the same wall: bitrate reads
  back 0, no compression, and GET ctls can segfault).

  Instead of fighting the variadic ABI we control the bitrate the way the Opus
  API explicitly allows: the `max_data_bytes` argument to opus_encode() caps the
  per-frame output size, which caps the instantaneous bitrate. For a 20 ms frame
  the cap in bytes is `bitrate_bps / 8 * 0.020`. Verified on arm64: cap=40B →
  13 kbps, cap=60B → ~20 kbps, with clean decode roundtrip. This needs zero ctl
  calls, so it is robust across platforms.

WIRE FORMAT:
  Each broadcast audio frame is prefixed with a 1-byte codec tag so the client
  can tell PCM from Opus without a control-channel race:
    0x00 = raw Int16 PCM (legacy)
    0x01 = Opus packet (decode with the frontend OpusDecoder @ 48 kHz mono)
  See AUDIO_TAG_PCM / AUDIO_TAG_OPUS.
"""

import ctypes
import logging
import sys
from ctypes.util import find_library
from pathlib import Path, PureWindowsPath
from typing import Optional

logger = logging.getLogger(__name__)

# ── Codec tags (1-byte prefix on each /WSaudioRX binary frame) ──────
AUDIO_TAG_PCM = 0x00
AUDIO_TAG_OPUS = 0x01

# ── Opus constants ──────────────────────────────────────────────────
OPUS_APPLICATION_VOIP = 2048
OPUS_APPLICATION_AUDIO = 2049
OPUS_OK = 0

# RX audio: 48 kHz mono (required by Opus encoder — 44.1k is not supported).
# The FT-710 USB device runs at 44.1k native; audio_handler.py captures at 44.1k
# and explicitly resamples to 48k via audio_resample.py (numpy linear interp).
# TX (phone mic uplink) also 48 kHz through Opus, resampled to 44.1k for playback.
RX_RATE = 48000
RX_CHANNELS = 1
TX_RATE = 48000
TX_CHANNELS = 1
FRAME_MS = 20
FRAME_SAMPLES = RX_RATE * FRAME_MS // 1000   # 960 @ 48 kHz
TX_MAX_FRAME_SAMPLES = 5760  # 120 ms max Opus frame @ 48 kHz (worst case)
TX_FRAME_SAMPLES = TX_RATE * FRAME_MS // 1000  # 960 @ 48 kHz
MAX_PACKET_BYTES = 4000   # output buffer ceiling (never reached in practice)

# Bitrate is controlled via the opus_encode max_data_bytes cap (see module
# docstring). cap_bytes = bitrate_bps / 8 * (FRAME_MS/1000). At 48 kHz fullband,
# Opus needs ≳32 kbps to even reach the high end; 48 kbps is audible-but-not-
# transparent on music. 64 kbps mono is the sweet spot for remote WFM listening:
# near-transparent on broadcast music yet only 1/12 the 768 kbps Int16 PCM rate,
# so a remote link stops underrunning (the PCM stutter). Runtime-adjustable via
# the setOpusBitrate control command (48/64/96/128 kbps presets on the client).
DEFAULT_BITRATE = 64000
MIN_BITRATE = 8000
MAX_BITRATE = 128000


class _OpusEncoder(ctypes.Structure):
    pass


_EncoderPtr = ctypes.POINTER(_OpusEncoder)


def _resource_roots() -> list[Path]:
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parent)

    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _libopus_candidates(
    platform: str | None = None,
    resource_roots: list[Path] | None = None,
    find_library_result: str | None = None,
) -> list[object]:
    platform = platform or sys.platform
    name = find_library_result if find_library_result is not None else find_library("opus")
    candidates: list[object] = [name] if name else []
    roots = resource_roots if resource_roots is not None else _resource_roots()

    if platform == "win32":
        for root in roots:
            win_root = PureWindowsPath(str(root))
            candidates.extend([
                win_root / "opus.dll",
                win_root / "_internal" / "opus.dll",
                win_root / "vendor" / "opus" / "windows" / "bin" / "x64" / "opus.dll",
            ])
        candidates.extend(["opus.dll", "libopus.dll"])
    elif platform == "darwin":
        for root in roots:
            candidates.extend([
                root / "libopus.dylib",
                root / "_internal" / "libopus.dylib",
                root / "vendor" / "opus" / "macos" / "libopus.dylib",
            ])
        candidates += [
            "/opt/homebrew/lib/libopus.dylib",
            "/usr/local/lib/libopus.dylib",
        ]
    else:
        for root in roots:
            candidates.extend([
                root / "libopus.so.0",
                root / "libopus.so",
                root / "_internal" / "libopus.so.0",
                root / "_internal" / "libopus.so",
            ])
        candidates += ["libopus.so.0", "libopus.so"]

    return candidates


def _load_libopus():
    """Locate and bind libopus, declaring argtypes so arm64 ctls work."""
    lib = None
    for c in _libopus_candidates():
        if not c:
            continue
        try:
            lib = ctypes.CDLL(str(c))
            break
        except OSError:
            continue
    if lib is None:
        raise OSError("libopus not found")

    lib.opus_encoder_create.argtypes = (
        ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int))
    lib.opus_encoder_create.restype = _EncoderPtr

    lib.opus_encoder_destroy.argtypes = (_EncoderPtr,)
    lib.opus_encoder_destroy.restype = None

    lib.opus_encode.argtypes = (
        _EncoderPtr, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
        ctypes.c_char_p, ctypes.c_int32)
    lib.opus_encode.restype = ctypes.c_int32

    # TX Decoder bindings
    lib.opus_decoder_create.argtypes = (ctypes.c_int, ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_int))
    lib.opus_decoder_create.restype = _DecoderPtr
    lib.opus_decoder_destroy.argtypes = (_DecoderPtr,)
    lib.opus_decoder_destroy.restype = None
    lib.opus_decode.argtypes = (
        _DecoderPtr, ctypes.c_char_p, ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int)
    lib.opus_decode.restype = ctypes.c_int32
    return lib


# ── TX Opus Decoder (phone→server mic uplink) ─────────────────────

class _OpusDecoder(ctypes.Structure):
    pass


_DecoderPtr = ctypes.POINTER(_OpusDecoder)


class TxOpusDecoder:
    """Stateful 48 kHz mono Opus decoder for TX mic uplink.

    Feed it Opus packets via ``decode(data)``; returns Int16 PCM bytes
    (resampled 48→44.1 kHz by audio_handler before playback).
    """

    def __init__(self):
        self._lib = _load_libopus()
        err = ctypes.c_int()
        self._dec = self._lib.opus_decoder_create(
            TX_RATE, TX_CHANNELS, ctypes.byref(err))
        if err.value != OPUS_OK or not self._dec:
            raise OSError(f"opus_decoder_create failed: {err.value}")
        self._pcm_buf = (ctypes.c_int16 * TX_MAX_FRAME_SAMPLES)()
        logger.info("TX Opus decoder ready: %d Hz mono", TX_RATE)

    def decode(self, opus_data: bytes) -> bytes:
        """Decode one Opus packet → Int16 PCM bytes."""
        n = self._lib.opus_decode(
            self._dec,
            opus_data, len(opus_data),
            self._pcm_buf, TX_MAX_FRAME_SAMPLES, 0)
        if n < 0:
            logger.warning("opus_decode failed: %d", n)
            return b''
        # ctypes buffer → raw bytes: cast address to (c_char * size)
        size = n * 2  # Int16 = 2B/sample
        return ctypes.string_at(ctypes.addressof(self._pcm_buf), size)

    def close(self):
        if self._dec:
            self._lib.opus_decoder_destroy(self._dec)
            self._dec = None


class RxOpusEncoder:
    """Stateful 48 kHz mono Opus encoder with a sample accumulator.

    Feed it arbitrary-length Int16 PCM via `push()`; it returns a list of Opus
    packets (one per complete 960-sample / 20 ms frame). Partial tails are kept
    buffered for the next call so frame boundaries stay continuous.
    """

    def __init__(self, bitrate: int = 24000):
        self._lib = _load_libopus()
        err = ctypes.c_int()
        self._enc = self._lib.opus_encoder_create(
            RX_RATE, RX_CHANNELS, OPUS_APPLICATION_AUDIO, ctypes.byref(err))
        if err.value != OPUS_OK or not self._enc:
            raise OSError(f"opus_encoder_create failed: {err.value}")

        self._tail = b""   # leftover Int16 PCM bytes (< 1 frame)
        self._outbuf = (ctypes.c_char * MAX_PACKET_BYTES)()
        self.set_bitrate(bitrate)   # sets self._bitrate + self._cap
        logger.info("RX Opus encoder ready: %d Hz mono, %d bps (cap %d B/frame)",
                    RX_RATE, self._bitrate, self._cap)

    @property
    def bitrate(self) -> int:
        return self._bitrate

    def set_bitrate(self, bitrate: int):
        """Control rate via the per-frame output byte cap (opus_encode's
        max_data_bytes). This avoids the variadic opus_encoder_ctl, whose
        arm64 macOS calling convention ctypes can't satisfy (SET ctls return
        BAD_ARG). bytes/frame = bitrate * FRAME_MS / 1000 / 8."""
        bitrate = max(MIN_BITRATE, min(MAX_BITRATE, int(bitrate)))
        self._bitrate = bitrate
        self._cap = max(16, min(MAX_PACKET_BYTES,
                                bitrate * FRAME_MS // 1000 // 8))

    def push(self, pcm: bytes) -> list[bytes]:
        """Accumulate Int16 PCM and return complete Opus packets."""
        buf = self._tail + pcm
        frame_bytes = FRAME_SAMPLES * 2
        packets: list[bytes] = []
        off = 0
        n = len(buf)
        while n - off >= frame_bytes:
            frame = buf[off:off + frame_bytes]
            off += frame_bytes
            # Cast bytes → pointer correctly: create a ctypes array from the
            # bytes buffer, then cast to an int16 pointer.  ctypes.cast() does
            # NOT accept raw Python bytes — it needs a ctypes object.
            nsamples = len(frame) // 2
            c_arr = (ctypes.c_int16 * nsamples).from_buffer_copy(frame)
            pcm_ptr = ctypes.cast(c_arr, ctypes.POINTER(ctypes.c_int16))
            ret = self._lib.opus_encode(
                self._enc, pcm_ptr, FRAME_SAMPLES,
                self._outbuf, self._cap)
            if ret < 0:
                logger.warning("opus_encode failed: %d", ret)
                continue
            packets.append(bytes(self._outbuf[:ret]))
        self._tail = buf[off:]
        return packets

    def reset(self):
        self._tail = b""

    def close(self):
        if getattr(self, "_enc", None):
            self._lib.opus_encoder_destroy(self._enc)
            self._enc = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
