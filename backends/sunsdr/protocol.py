"""SunSDR2 DX — pure UDP protocol layer (native backend, Task 1).

Byte-for-byte port of the protocol half of
``/Users/cheenle/HAM/sunsdr/web_control/sunsdr_direct.py`` plus the RX-side
parsing from ``sunmrrc/server.py`` (0x1F00/0x1F01 telemetry, 24-bit IQ).
Pure functions and data only: no socket, no asyncio — the I/O half lands in a
later task.

Wire format (PROTOCOL.md §2): every control packet is a 14-byte little-endian
header ``<HHIIH`` (magic 0xFF32, cmd_id, data_len, index 0x00010000, reserved
0) + payload + a 4-byte trailing word. The trailing word is NOT a CRC; its
meaning is per-command (PTT state, drive byte, STREAM_CTRL config value).

Field-placement gotchas carried over from the reference (both were real
field bugs):

- DRIVE (0x0017): the power byte lives in the TRAILING word, payload stays
  ``uint32 0``. Payload-carried drive transmits at ~zero power.
- STREAM_CTRL (0x0020): word[1] must be 0. ExpertSDR3 sends 1; on SunSDR2
  that killed TX power (verified 2025-06-24).
- HW_INIT (0x0001): trailing is 0x37E00000 (capture-verified); the IQ sample
  rate is word[11] (0=39k, 1=78k, 2=156k, 3=312k), word[10]=word[11]*65536+1.
- Telemetry 0x1F00 carries forward/avg power (W), supply voltage (x10) and
  PA temp — there is NO reverse-power field, so no SWR is ever reported.
"""
from __future__ import annotations

import struct
from typing import Optional, Union

import numpy as np

MAGIC = 0xFF32
TRX_INDEX = 0x00010000
IF_OFFSET_HZ = 30500.0          # RX DDS = (VFO + IF_OFFSET) * 10
IQ_SCALE = 8388608.0            # 2^23 — 24-bit signed full scale
IQ_SAMPLES_PER_PACKET = 200
IQ_PAYLOAD_BYTES = 1200         # 200 samples x (3 bytes I + 3 bytes Q)
IQ_PACKET_BYTES = 1210          # 10-byte stream header + 1200 payload
IQ_SUBTYPE = 0xFFFE
TELEM_POWER_SUBTYPE = 0x1F00    # 34 bytes: power / volts / PA temp
TELEM_STATUS_SUBTYPE = 0x1F01   # 22 bytes: TX-status flags
PTT_CONFIRM_BIT = 0x00010000    # trailing-word bit 16 of 0x1F01 during TX
STREAM_COUNTER_START = 0x04B0
# Counters are uint32 on the wire: an engine incrementing these MUST mask
# with & 0xFFFFFFFF before packing (the reference server does exactly that;
# an unmasked Python int would eventually overflow struct "<I").
STREAM_COUNTER_STEP = 0x10000

_HEADER = struct.Struct("<HHIIH")
_STREAM_HEADER = struct.Struct("<HHIH")

# IQ sample rates: multiples of 5^7 (78125). Selected by HW_INIT word[11],
# NOT by STREAM_CTRL (rate changes need a full re-boot — see PROTOCOL.md §4.3).
SAMPLE_RATES: dict[str, int] = {
    "39k": 78125 // 2,          # 39062
    "78k": 78125,               # verified default
    "156k": 78125 * 2,          # 156250
    "312k": 78125 * 4,          # 312500
}
HW_INIT_RATE_INDEX: dict[str, int] = {"39k": 0, "78k": 1, "156k": 2, "312k": 3}
DEFAULT_RATE = "78k"

# 0x0020 STREAM_CTRL fixed payload (13 u32 LE words, rate-independent).
# word[1] = 0 matches the original 78k capture that had working TX.
STREAM_CTRL_WORDS = (0, 0, 1, 0, 0, 100, 0, 0, 30, 700, 7, 100, 300)
STREAM_CTRL_TRAILING = 0x64     # 100

# 0x0001 HW_INIT fixed trailing (capture-verified; see module docstring).
HW_INIT_TRAILING = 0x37E00000
# Boot-phase DRIVE byte captured from ExpertSDR3 (re-sent per band at runtime).
BOOT_DRIVE_BYTE = 0xDC


class CmdID:
    """Control-port (50001) command identifiers (PROTOCOL.md §3.1)."""
    HW_INIT      = 0x0001   # Hardware init + IQ sample rate selector
    START_OPS    = 0x0002   # Start Operation — MUST be the first command
    SET_PARAM_5  = 0x0005   # Mode/attenuator/preamp config (trailing = level)
    PTT          = 0x0006   # payload=0, trailing word: 1=TX / 0=RX
    SET_PARAM_7  = 0x0007   # Extended config (26 bytes)
    RX_FREQ      = 0x0008   # RX DDS = (VFO + IF_OFFSET) x 10
    TX_FREQ      = 0x0009   # TX VFO x 10 (exact, no IF offset)
    INFO_QUERY   = 0x000E   # Device info (486-byte response)
    CALIB_QUERY  = 0x0010   # Calibration data (486-byte response)
    SET_PARAM_15 = 0x0015   # Parameter set
    DRIVE        = 0x0017   # TX power: trailing byte = round(255*sqrt(pct/100))
    HEARTBEAT    = 0x0018   # Keep-alive every 0.5s
    SET_PARAM_19 = 0x0019   # Parameter set
    SET_PARAM_1B = 0x001B   # Parameter set
    SET_PARAM_1D = 0x001D   # Parameter set
    SET_PARAM_1E = 0x001E   # Parameter set
    STREAM_CTRL  = 0x0020   # Start IQ stream (52-byte payload, trailing=100)
    SET_PARAM_21 = 0x0021   # Parameter set
    SET_PARAM_22 = 0x0022   # Parameter set (12 bytes)
    SET_PARAM_24 = 0x0024   # Parameter set
    SET_PARAM_26 = 0x0026   # Parameter set
    VOX_CTRL     = 0x0027   # VOX config (16 bytes)
    STATUS       = 0x005A   # Status query
    PRE_CONFIG   = 0x005F   # Pre-boot handshake (6 bytes)


def build_packet(cmd_id: int, data: bytes = b"",
                 index: int = TRX_INDEX, trailing: int = 0) -> bytes:
    """Build a 14-byte header + payload + 4-byte trailing word."""
    hdr = _HEADER.pack(MAGIC, cmd_id, len(data), index, 0)
    return hdr + data + struct.pack("<I", trailing)


def build_ptt(tx: bool) -> bytes:
    """PTT: payload is uint32 0, the trailing word carries the state."""
    return build_packet(CmdID.PTT, struct.pack("<I", 0), trailing=1 if tx else 0)


def drive_byte(percent: Union[int, float]) -> int:
    """Square-root taper drive byte: round(255 * sqrt(pct/100)), clamped."""
    pct = max(0, min(100, percent))
    return int(round(255 * (pct / 100.0) ** 0.5))


def build_drive(percent: Union[int, float]) -> bytes:
    """DRIVE (0x0017). The byte goes in the TRAILING word, payload stays 0."""
    return build_packet(CmdID.DRIVE, struct.pack("<I", 0),
                        trailing=drive_byte(percent))


def build_rx_frequency(freq_hz: Union[int, float]) -> bytes:
    """RX_FREQ (0x0008): DDS = (freq + 30500 Hz IF offset) x 10, integer."""
    dds = int((float(freq_hz) + IF_OFFSET_HZ) * 10)
    return build_packet(CmdID.RX_FREQ, struct.pack("<II", 0, dds))


def build_tx_frequency(freq_hz: Union[int, float]) -> bytes:
    """TX_FREQ (0x0009): exact VFO x 10, integer."""
    vfo = int(float(freq_hz) * 10)
    return build_packet(CmdID.TX_FREQ, struct.pack("<II", 0, vfo))


def _rate_index(rate: Union[str, int]) -> int:
    """Normalize a rate key (``"78k"``) or an integer rate (78125) to 0-3."""
    if isinstance(rate, str):
        try:
            return HW_INIT_RATE_INDEX[rate]
        except KeyError:
            raise ValueError(f"unknown IQ sample rate key: {rate!r}") from None
    for key, hz in SAMPLE_RATES.items():
        if hz == rate:
            return HW_INIT_RATE_INDEX[key]
    raise ValueError(f"unknown IQ sample rate: {rate!r}")


def build_hw_init(rate: Union[str, int] = DEFAULT_RATE) -> bytes:
    """HW_INIT (0x0001) — calibration blob + IQ sample rate selector.

    Payload is exactly 50 bytes (12 u32 words + 2-byte zero pad): words 1-8
    are the calibration value 50, word[10] = index*65536+1, word[11] = index.
    Trailing is the capture-verified 0x37E00000. Must be sent BEFORE the
    frequency commands and requires a full re-boot to change the rate.
    """
    index = _rate_index(rate)
    words = [0, 50, 50, 50, 50, 50, 50, 50, 50, 0,
             index * 65536 + 1, index]
    payload = struct.pack("<%dI" % len(words), *words) + b"\x00\x00"
    return build_packet(CmdID.HW_INIT, payload, trailing=HW_INIT_TRAILING)


def build_stream_ctrl() -> bytes:
    """STREAM_CTRL (0x0020) — starts the IQ stream. Fixed, rate-independent.

    word[1]=0 is load-bearing: ExpertSDR3 sends word[1]=1, but that value
    killed TX power on the SunSDR2 (verified). Guard it so a future refactor
    against an ExpertSDR3 capture can't silently regress TX power to ~0.
    """
    if STREAM_CTRL_WORDS[1] != 0:
        raise AssertionError(
            "STREAM_CTRL word[1] must stay 0 — word[1]=1 kills SunSDR2 TX "
            "power (capture-verified, see module docstring)")
    payload = struct.pack("<%dI" % len(STREAM_CTRL_WORDS), *STREAM_CTRL_WORDS)
    return build_packet(CmdID.STREAM_CTRL, payload, trailing=STREAM_CTRL_TRAILING)


# ── Captured boot sequence (fixed frames) ─────────────────────────
# Reference: SunSDR2DXClient._send_boot_sequence() — the only dynamic
# elements are HW_INIT (rate), the frequency packets and the drive byte;
# everything else is fixed hex from the ExpertSDR3 1.0.17 capture.

BOOT_PHASE1_HEX = (
    "32ff0200040000000000010000000000000000000000",   # START_OPS — first
    # PRE_CONFIG: verbatim capture, 24 B on the wire (14 header + data_len 6
    # + 4 trailing), payload and trailing all zero. Do NOT trim or pad it.
    "32ff5f000600000000000100000000000000000000000000",
    "32ff5f000600000000000100000000000000000000000000",
    "32ff5f000600000000000100000000000000000000000000",
    "32ff1d00040000000000010000000000000000000000",     # SET_PARAM_1D
    "32ff1b00040000000000010000000000000000000000",     # SET_PARAM_1B
    # SET_PARAM_5: trailing=1 matches ExpertSDR3 boot (PROTOCOL.md §21.1).
    "32ff0500040000000000010000000000000001000000",
    "32ff1800040000000000010000000000000000000000",     # HEARTBEAT
    "32ff19000400000000000100000000000000bb000000",     # SET_PARAM_19 (0xBB)
    "32ff2100040000000000010000000000000001000000",     # SET_PARAM_21 (=1)
)

BOOT_STATUS_HEX = (
    "32ff5a000000000000000100000000000000",             # STATUS x5
    "32ff5a000000000000000100000000000000",
    "32ff5a000000000000000100000000000000",
    "32ff5a000000000000000100000000000000",
    "32ff5a000000000000000100000000000000",
)

# Stream-start phase, in order. Entry [0] is the boot DRIVE (0xDC); the boot
# builder re-creates it from its drive_byte argument, the remaining four are
# used verbatim.
BOOT_STREAM_PRE_HEX = (
    "32ff17000400000000000100000000000000dc000000",     # DRIVE (boot 0xDC)
    "32ff1e00040000000000010000000000000000000000",     # SET_PARAM_1E
    "32ff1500040000000000010000000000000001000000",     # SET_PARAM_15 (=1)
    "32ff07001a00000000000100000000000000"              # SET_PARAM_7 (26B)
    "0000000000000000000000000000000000000000000000000000",
    "32ff2400040000000000010000000000000000000000",     # SET_PARAM_24
)

BOOT_STREAM_POST_HEX = (
    "32ff1800040000000000010000000000000000000000",     # HEARTBEAT
    "32ff2600040000000000010000000000000000000000",     # SET_PARAM_26
    "32ff27001000000000000100000000000000"              # VOX_CTRL (16B)
    "dc460300b6d20000dc460300b6d20000",
    "32ff22000c00000000000100000000000000"              # SET_PARAM_22 (12B)
    "000000000084d71700000000",
)


def build_boot_sequence(freq_hz: Union[int, float],
                        rate: Union[str, int] = DEFAULT_RATE,
                        drive: int = BOOT_DRIVE_BYTE,
                        att_preamp: int = 1) -> list[bytes]:
    """The full boot sequence as a list of packets, in captured order.

    29 packets: 10 pre-config + 5 status queries + HW_INIT + TX/RX/RX freq +
    DRIVE (rebuilt from ``drive``) + 4 fixed post-init + STREAM_CTRL + 4 fixed.
    Pure data — the caller owns spacing (30 ms) and transport.
    """
    seq = [bytes.fromhex(h) for h in BOOT_PHASE1_HEX]
    # Preserve the operator's combined ATT/PRE selection across a re-boot;
    # the captured -10 dB value is only the default for this builder.
    seq[6] = build_packet(CmdID.SET_PARAM_5, struct.pack("<I", 0),
                          trailing=max(0, min(3, int(att_preamp))))
    seq += [bytes.fromhex(h) for h in BOOT_STATUS_HEX]
    seq.append(build_hw_init(rate))
    seq.append(build_tx_frequency(freq_hz))
    seq.append(build_rx_frequency(freq_hz))
    seq.append(build_rx_frequency(freq_hz))          # sent twice on purpose
    seq.append(build_packet(CmdID.DRIVE, struct.pack("<I", 0),
                            trailing=max(0, min(255, int(drive)))))
    seq += [bytes.fromhex(h) for h in BOOT_STREAM_PRE_HEX[1:]]
    seq.append(build_stream_ctrl())
    seq += [bytes.fromhex(h) for h in BOOT_STREAM_POST_HEX]
    return seq


# ── 24-bit IQ ─────────────────────────────────────────────────────

def pack_iq_24bit(samples) -> bytes:
    """Vectorized pack of complex samples to 24-bit interleaved LE bytes.

    Per sample: I then Q, each a signed 24-bit two's-complement little-endian
    integer scaled by 2^23 (full scale = +/-1.0, saturating). 200 samples ->
    1200 bytes.
    """
    arr = np.asarray(samples, dtype=np.complex64).reshape(-1)
    n = arr.shape[0]
    # nan_to_num keeps the int cast deterministic: NaN -> code 0, +/-inf
    # saturate via the clip below (NaN would otherwise cast to an
    # implementation-defined int64 and raise an invalid-value warning).
    iq = np.empty((n, 2), dtype=np.int64)
    for col, part in ((0, arr.real), (1, arr.imag)):
        scaled = np.nan_to_num(np.rint(part.astype(np.float64) * IQ_SCALE),
                               nan=0.0)
        iq[:, col] = np.clip(scaled, -8388608, 8388607)
    u32 = (iq & 0xFFFFFF).astype("<u4")
    block = np.frombuffer(u32.tobytes(), dtype=np.uint8).reshape(-1, 4)
    return block[:, :3].tobytes()


def unpack_iq_24bit(data: bytes) -> np.ndarray:
    """Decode interleaved 24-bit LE I/Q to complex64 (full scale = 2^23).

    Raises ValueError unless ``len(data)`` is an exact multiple of 6.
    """
    raw = bytes(data)
    if len(raw) % 6 != 0:
        raise ValueError(
            f"IQ payload length must be a multiple of 6, got {len(raw)}")
    if not raw:
        return np.zeros(0, dtype=np.complex64)
    g = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 6)

    def _dec(tri: np.ndarray) -> np.ndarray:
        v = (tri[:, 0].astype(np.int32)
             | (tri[:, 1].astype(np.int32) << 8)
             | (tri[:, 2].astype(np.int32) << 16))
        return np.where(tri[:, 2] & 0x80, v - (1 << 24), v)

    i = _dec(g[:, :3]).astype(np.float32) / np.float32(IQ_SCALE)
    q = _dec(g[:, 3:6]).astype(np.float32) / np.float32(IQ_SCALE)
    return (i + 1j * q).astype(np.complex64)


# ── RX packet / telemetry parsing (never raises) ──────────────────

def parse_telemetry(raw: bytes) -> Optional[dict]:
    """Parse a 0x1F00 power or 0x1F01 status packet from the device.

    Offsets verified 2026-06-25 against a real ExpertSDR3 40m drive sweep.
    0x1F00 (34 B): forward_w f32@30, avg_w f32@22, volts u16@16 / 10,
    pa_temp_c f32@18. The device sends NO reverse-power field, so there is
    no SWR — never fabricate one. 0x1F01 (22 B): trailing u32 bit 0x10000
    set while the device is in TX. Returns None on anything malformed.
    """
    try:
        if len(raw) < _STREAM_HEADER.size:
            return None
        magic, sub = struct.unpack_from("<HH", raw, 0)
        if magic != MAGIC:
            return None
        if sub == TELEM_POWER_SUBTYPE:
            if len(raw) < 34:
                return None
            volts_raw = struct.unpack_from("<H", raw, 16)[0]
            pa_temp = struct.unpack_from("<f", raw, 18)[0]
            avg_w = struct.unpack_from("<f", raw, 22)[0]
            fwd_w = struct.unpack_from("<f", raw, 30)[0]
            return {
                "forward_w": float(fwd_w),
                "avg_w": float(avg_w),
                "volts": volts_raw / 10.0,
                "pa_temp_c": float(pa_temp),
            }
        if sub == TELEM_STATUS_SUBTYPE:
            if len(raw) < 22:
                return None
            status = struct.unpack_from("<I", raw, len(raw) - 4)[0]
            return {"ptt": bool(status & PTT_CONFIRM_BIT)}
        return None
    except (struct.error, ValueError, TypeError):
        # Expected parse failures only (truncated buffer, bad value/type).
        # A programming error must propagate, not be swallowed as "no data".
        return None


def parse_rx_packet(raw: bytes) -> Optional[dict]:
    """Classify and parse one device -> PC stream packet (port 50002).

    Stream header is ``<HHIH``: magic, sub_type, packet counter, flags
    (bytes 4-7 are a COUNTER on stream packets, not data_len). IQ packets
    (0xFFFE) carry interleaved 24-bit samples; 0x1F00/0x1F01 route to
    :func:`parse_telemetry`. Returns None on anything malformed — a UDP
    listener must never die on a bad packet.
    """
    try:
        if len(raw) < _STREAM_HEADER.size:
            return None
        magic, sub, counter, flags = _STREAM_HEADER.unpack_from(raw, 0)
        if magic != MAGIC:
            return None
        if sub == IQ_SUBTYPE:
            payload = raw[_STREAM_HEADER.size:]
            if len(payload) < 6 or len(payload) % 6 != 0:
                return None
            return {
                "kind": "iq",
                "counter": counter,
                "flags": flags,
                "samples": unpack_iq_24bit(payload),
            }
        telem = parse_telemetry(raw)
        if telem is None:
            return None
        return {"kind": "telemetry", **telem}
    except (struct.error, ValueError, TypeError):
        return None
